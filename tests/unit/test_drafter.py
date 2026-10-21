from decimal import Decimal

from core.agent.drafter import MAX_CANDIDATES, draft_comments, system_prompt
from core.cost import CostMeter
from core.errors import LLMOutputError


def comment(category="bug", confidence=0.9, line=1):
    return {"path": "a.py", "line": line, "category": category, "severity": "low",
            "claim": "c", "body": "b", "confidence": confidence}


class ScriptedLLM:
    """Returns a fixed answer per prompt file, chosen by a word only that prompt contains."""

    def __init__(self, by_focus):
        self.by_focus = by_focus
        self.prompts = []

    async def structured(self, purpose, system, user, schema, meter, max_tokens=4000):
        self.prompts.append(system)
        for marker, answer in self.by_focus.items():
            if marker in system:
                if isinstance(answer, Exception):
                    raise answer
                return schema.model_validate({"comments": answer})
        return schema.model_validate({"comments": []})


METER = CostMeter(Decimal("1"))


async def test_one_call_per_category_group():
    llm = ScriptedLLM({})
    await draft_comments(llm, METER, "ctx")
    assert len(llm.prompts) == 3


async def test_low_confidence_is_dropped_and_sorted():
    llm = ScriptedLLM({'"bug" and "perf"': [comment(confidence=0.3), comment(confidence=0.5),
                                             comment(confidence=0.95)]})
    result = await draft_comments(llm, METER, "ctx")
    assert [c.confidence for c in result] == [0.95, 0.5]


async def test_candidates_are_capped():
    llm = ScriptedLLM({'"bug" and "perf"': [comment(line=i) for i in range(1, 16)],
                       '"security" only': [comment("security", line=i) for i in range(1, 6)]})
    assert len(await draft_comments(llm, METER, "ctx")) == MAX_CANDIDATES


async def test_broken_group_is_skipped_not_fatal():
    llm = ScriptedLLM({'"security" only': LLMOutputError("garbage"),
                       '"bug" and "perf"': [comment()]})
    assert len(await draft_comments(llm, METER, "ctx")) == 1


async def test_disabled_categories_are_not_requested():
    llm = ScriptedLLM({})
    await draft_comments(llm, METER, "ctx", categories=frozenset({"security"}))
    assert len(llm.prompts) == 1


def test_system_prompt_includes_focus_and_schema():
    prompt = system_prompt("draft_security.md")
    assert '"security" only' in prompt
    assert '"confidence"' in prompt and "{schema}" not in prompt
