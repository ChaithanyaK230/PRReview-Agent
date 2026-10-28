"""Reflection loop (LLD §13.3): critics check, the reviser repairs, up to MAX_ITERATIONS rounds."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

import structlog

from core.agent.schemas import CandidateComment, ReviseResponse
from core.critics.anchor import AnchorCritic
from core.critics.base import Attempt, Critic, CriticContext, CriticVerdict
from core.critics.patch import PatchApplyCritic, PatchLintCritic, PatchTestsCritic

log = structlog.get_logger()

# Cheapest first, so most failures are caught before any sandbox time is spent (LLD §13.2).
# TODO: grounding (20) and duplicate_finding (30); bug_proof (70) arrives in Step 23.
DEFAULT_CRITICS: list[Critic] = [AnchorCritic(), PatchApplyCritic(), PatchLintCritic(), PatchTestsCritic()]
PARALLEL_CANDIDATES = 2  # per sandbox; each has its own scratch folder


class ReviserLike(Protocol):
    async def revise(self, comment: CandidateComment, failures: list[CriticVerdict]) -> ReviseResponse: ...


@dataclass
class Reflection:
    candidate_id: uuid.UUID
    comment: CandidateComment  # final version
    status: Literal["accepted", "dropped"] = "dropped"
    iterations: int = 0
    history: list[dict] = field(default_factory=list)  # earlier versions, for the trace viewer
    verdicts: list[tuple[int, CriticVerdict]] = field(default_factory=list)  # (iteration, verdict)
    patch_verified: bool = False  # the suggested fix applied and kept tests green
    verified: bool = False  # bug proven by a test (Step 23)
    drop_reason: str = ""


async def reflect_one(
    candidate_id: uuid.UUID, comment: CandidateComment, critics: list[Critic], ctx: CriticContext,
    reviser: ReviserLike, max_iterations: int,
) -> Reflection:
    out = Reflection(candidate_id, comment)
    for i in range(max_iterations):
        out.iterations = i + 1
        round_verdicts: list[CriticVerdict] = []
        for critic in critics:
            if not critic.applies(out.comment, ctx):
                continue
            started = time.perf_counter()
            out.comment, verdict = await critic.check(out.comment, ctx, Attempt(candidate_id, i))
            verdict = verdict.model_copy(update={"duration_ms": int((time.perf_counter() - started) * 1000)})
            round_verdicts.append(verdict)
            out.verdicts.append((i, verdict))
            if not verdict.passed:
                break  # short-circuit: no point testing a patch that doesn't apply

        if all(v.passed for v in round_verdicts):
            out.status = "accepted"
            ran = {v.critic for v in round_verdicts if not v.feedback.startswith("skipped")}
            out.patch_verified = {"patch_apply", "patch_tests"} <= ran
            out.verified = any(v.verified for v in round_verdicts)
            return out

        if i + 1 == max_iterations:
            out.drop_reason = f"still failing after {max_iterations} rounds"
            break
        revision = await reviser.revise(out.comment, [v for v in round_verdicts if not v.passed])
        if revision.action == "drop" or revision.comment is None:
            out.drop_reason = f"reviser dropped it: {revision.reason}"
            break
        out.history.append(out.comment.model_dump())
        out.comment = revision.comment

    log.info("reflect.dropped", candidate=str(candidate_id), reason=out.drop_reason)
    return out


async def reflect_all(
    items: list[tuple[uuid.UUID, CandidateComment]], ctx: CriticContext, reviser: ReviserLike,
    max_iterations: int, critics: list[Critic] | None = None,
) -> list[Reflection]:
    gate = asyncio.Semaphore(PARALLEL_CANDIDATES)

    async def one(cid: uuid.UUID, comment: CandidateComment) -> Reflection:
        async with gate:
            return await reflect_one(cid, comment, critics or DEFAULT_CRITICS, ctx, reviser, max_iterations)

    return list(await asyncio.gather(*(one(cid, c) for cid, c in items)))
