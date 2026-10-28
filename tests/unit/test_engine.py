"""Reflection loop logic with scripted critics and a scripted reviser (no sandbox, no LLM)."""

import uuid

from core.agent.schemas import CandidateComment, ReviseResponse
from core.analysis.config import RepoConfig
from core.critics.base import CriticContext, CriticVerdict
from core.critics.engine import reflect_all, reflect_one
from core.github.diffmap import DiffMap

CID = uuid.UUID(int=7)
CTX = CriticContext(diff=DiffMap.from_diff(""), config=RepoConfig())


def cc(line=4, claim="add subtracts"):
    return CandidateComment(path="calc.py", line=line, category="bug", severity="high",
                            claim=claim, body="b", confidence=0.9)


class LineCritic:
    """Passes only when the comment is on line 4."""

    name = "anchor"

    def __init__(self):
        self.seen = []

    def applies(self, c, ctx):
        return True

    async def check(self, c, ctx, attempt):
        self.seen.append((attempt.iteration, c.line))
        ok = c.line == 4
        return c, CriticVerdict(critic=self.name, passed=ok, feedback="" if ok else f"line {c.line} not in diff")


class ScriptedCritic:
    def __init__(self, name, passed=True, applies=True, feedback="", verified=False):
        self.name, self._passed, self._applies = name, passed, applies
        self._feedback, self._verified, self.calls = feedback, verified, 0

    def applies(self, c, ctx):
        return self._applies

    async def check(self, c, ctx, attempt):
        self.calls += 1
        return c, CriticVerdict(critic=self.name, passed=self._passed, feedback=self._feedback,
                                verified=self._verified)


class ScriptedReviser:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen_failures = []

    async def revise(self, comment, failures):
        self.seen_failures.append([v.critic for v in failures])
        return self.responses.pop(0)


async def test_passes_first_round():
    out = await reflect_one(CID, cc(), [LineCritic()], CTX, ScriptedReviser(), 3)
    assert (out.status, out.iterations, out.history) == ("accepted", 1, [])


async def test_failure_is_revised_then_accepted():
    reviser = ScriptedReviser(ReviseResponse(action="revise", comment=cc(line=4), reason="moved"))
    critic = LineCritic()
    out = await reflect_one(CID, cc(line=40), [critic], CTX, reviser, 3)
    assert (out.status, out.iterations) == ("accepted", 2)
    assert out.history[0]["line"] == 40              # the rejected draft is kept for the trace
    assert critic.seen == [(0, 40), (1, 4)]
    assert reviser.seen_failures == [["anchor"]]     # reviser only sees what failed


async def test_reviser_can_drop():
    out = await reflect_one(CID, cc(line=40), [LineCritic()], CTX,
                            ScriptedReviser(ReviseResponse(action="drop", reason="claim was wrong")), 3)
    assert out.status == "dropped" and "claim was wrong" in out.drop_reason


async def test_gives_up_after_max_iterations():
    bad = ReviseResponse(action="revise", comment=cc(line=41), reason="try again")
    out = await reflect_one(CID, cc(line=40), [LineCritic()], CTX, ScriptedReviser(bad, bad), 3)
    assert (out.status, out.iterations) == ("dropped", 3)
    assert len(out.verdicts) == 3


async def test_short_circuits_on_first_failure():
    later = ScriptedCritic("patch_tests")
    await reflect_one(CID, cc(), [ScriptedCritic("patch_apply", passed=False, feedback="does not apply"), later],
                      CTX, ScriptedReviser(ReviseResponse(action="drop", reason="x")), 3)
    assert later.calls == 0  # no point running tests for a patch that doesn't apply


async def test_patch_verified_needs_both_apply_and_tests_to_really_run():
    critics = [ScriptedCritic("patch_apply"), ScriptedCritic("patch_tests", feedback="skipped: no test results")]
    out = await reflect_one(CID, cc(), critics, CTX, ScriptedReviser(), 3)
    assert out.status == "accepted" and not out.patch_verified  # a skipped test check proves nothing

    out = await reflect_one(CID, cc(), [ScriptedCritic("patch_apply"), ScriptedCritic("patch_tests")],
                            CTX, ScriptedReviser(), 3)
    assert out.patch_verified


async def test_critics_that_dont_apply_are_skipped():
    out = await reflect_one(CID, cc(), [ScriptedCritic("patch_apply", applies=False)], CTX, ScriptedReviser(), 3)
    assert out.status == "accepted" and out.verdicts == [] and not out.patch_verified


async def test_reflect_all_keeps_candidates_separate():
    items = [(uuid.UUID(int=1), cc(line=4)), (uuid.UUID(int=2), cc(line=40))]
    drop = ReviseResponse(action="drop", reason="no")
    results = await reflect_all(items, CTX, ScriptedReviser(drop), 3, critics=[LineCritic()])
    assert [(r.candidate_id.int, r.status) for r in results] == [(1, "accepted"), (2, "dropped")]
