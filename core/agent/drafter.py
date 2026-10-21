import asyncio
import json
from pathlib import Path
from typing import Protocol

import structlog

from core.agent.schemas import CandidateComment, DraftResponse
from core.cost import CostMeter
from core.db.models import LLMPurpose
from core.errors import LLMOutputError

log = structlog.get_logger()

PROMPTS = Path(__file__).parent / "prompts"

# One LLM call per group, run concurrently (LLD §12.3)
GROUPS: list[tuple[str, frozenset[str]]] = [
    ("draft_bug.md", frozenset({"bug", "perf"})),
    ("draft_security.md", frozenset({"security"})),
    ("draft_tests.md", frozenset({"test_gap"})),
]
DEFAULT_CATEGORIES = frozenset({"bug", "security", "test_gap", "perf"})
MIN_CONFIDENCE = 0.4
MAX_CANDIDATES = 15  # LLD §17: candidates entering reflection


class StructuredLLM(Protocol):
    async def structured(self, purpose, system, user, schema, meter, max_tokens=4000): ...


def system_prompt(focus_file: str) -> str:
    base = (PROMPTS / "draft_system.md").read_text(encoding="utf-8")
    focus = (PROMPTS / focus_file).read_text(encoding="utf-8")
    # str.replace, not str.format: the JSON schema is full of braces
    return base.replace("{focus}", focus).replace("{schema}", json.dumps(DraftResponse.model_json_schema()))


async def draft_comments(
    llm: StructuredLLM, meter: CostMeter, context: str, categories: frozenset[str] = DEFAULT_CATEGORIES
) -> list[CandidateComment]:
    groups = [focus for focus, cats in GROUPS if cats & categories]
    results = await asyncio.gather(
        *(llm.structured(LLMPurpose.DRAFT, system_prompt(f), context, DraftResponse, meter) for f in groups),
        return_exceptions=True,
    )

    comments: list[CandidateComment] = []
    for focus, result in zip(groups, results):
        if isinstance(result, LLMOutputError):
            log.warning("draft.group_skipped", group=focus, error=str(result))  # one bad group doesn't sink the review
            continue
        if isinstance(result, BaseException):
            raise result
        comments += [c for c in result.comments if c.category in categories]

    kept = [c for c in comments if c.confidence >= MIN_CONFIDENCE]
    kept.sort(key=lambda c: c.confidence, reverse=True)
    log.info("draft.done", drafted=len(comments), kept=len(kept[:MAX_CANDIDATES]))
    return kept[:MAX_CANDIDATES]
