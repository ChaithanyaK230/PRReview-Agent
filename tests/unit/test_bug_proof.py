"""bug_proof critic: every row of the LLD §13.4 outcome table, with a fake sandbox and a fake prover."""

import uuid

from core.agent.prover import ProofTest, import_hint
from core.agent.schemas import CandidateComment
from core.analysis.config import RepoConfig
from core.analysis.detect import Toolchain
from core.analysis.models import TestResult, ToolOutcome
from core.analysis.runners.pytest import OUT_DIR
from core.analysis.workspace import SideSetup, Workspace
from core.critics.base import Attempt, CriticContext
from core.critics.proof import PROOF_PATH, BugProofCritic, classify
from core.github.diffmap import DiffMap
from core.sandbox.base import WORK_ROOT
from tests.fakes import FakeSandbox

ATTEMPT = Attempt(uuid.UUID(int=9), 0)
PROOF = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
ASSERT_FAIL = '<failure message="assert -1 == 5">E       assert -1 == 5</failure>'
IMPORT_ERROR = '<error message="collection failure">E   ModuleNotFoundError: No module named calcx</error>'


def cc(severity="high", **kw):
    base = dict(path="calc.py", line=2, category="bug", severity=severity, claim="add subtracts", body="b",
                confidence=0.9, evidence_needed="repro_test",
                suggested_patch="@@ -2 +2 @@\n-    return a - b\n+    return a + b\n")
    return CandidateComment(**{**base, **kw})


class FakeProver:
    def __init__(self, *contents):
        self.contents = list(contents) or [PROOF]
        self.calls = []

    async def write(self, comment, file_text, previous_error=None):
        self.calls.append(previous_error)
        return ProofTest(content=self.contents.pop(0) if len(self.contents) > 1 else self.contents[0])


def junit(inner):
    return (f'<testsuites><testsuite><testcase classname="test_prreview_proof" name="test_add" '
            f'file="{PROOF_PATH}">{inner}</testcase></testsuite></testsuites>').encode()


def setup(reports, prover=None, severity="high"):
    sbx = FakeSandbox()
    sbx.files[f"{WORK_ROOT}/head/calc.py"] = b"def add(a, b):\n    return a - b\n"
    for tag, inner in reports.items():
        sbx.files[f"{OUT_DIR}/proof-{ATTEMPT.candidate_id.hex[:12]}-0-{tag}-junit.xml"] = junit(inner)
    side = lambda n: SideSetup(n, f"{WORK_ROOT}/{n}", Toolchain("python", "none"), True, "", 0)  # noqa: E731
    ctx = CriticContext(diff=DiffMap.from_diff(""), config=RepoConfig(), sandbox=sbx,
                        workspace=Workspace(side("base"), side("head"), True), prover=prover or FakeProver())
    return ctx, sbx


async def check(ctx, comment=None):
    return await BugProofCritic().check(comment or cc(), ctx, ATTEMPT)


async def test_fails_on_head_passes_with_fix_is_verified():
    ctx, sbx = setup({"head1": ASSERT_FAIL, "fix1": ""})
    _, verdict = await check(ctx)
    assert verdict.passed and verdict.verified and verdict.proof_test == PROOF
    # the proof file goes where WE decide, in both the unpatched and the patched copy
    written = [p for p in sbx.files if p.endswith(PROOF_PATH)]
    assert len(written) == 2 and any("-proof/" in p for p in written)


async def test_fix_that_does_not_fix_goes_back_to_the_reviser():
    ctx, _ = setup({"head1": ASSERT_FAIL, "fix1": ASSERT_FAIL})
    _, verdict = await check(ctx)
    assert not verdict.passed and "does not fix it" in verdict.feedback


async def test_cannot_reproduce_lowers_severity():
    ctx, _ = setup({"head1": ""})  # the test passes on the PR's code: no bug shown
    comment, verdict = await check(ctx)
    assert verdict.passed and verdict.downgraded and not verdict.verified
    assert comment.severity == "medium"


async def test_cannot_reproduce_a_low_severity_claim_fails():
    ctx, _ = setup({"head1": ""})
    _, verdict = await check(ctx, cc(severity="low"))
    assert not verdict.passed and "drop it" in verdict.feedback


async def test_broken_test_gets_one_retry_with_the_error():
    prover = FakeProver(PROOF.replace("calc", "calcx"), PROOF)
    ctx, _ = setup({"head1": IMPORT_ERROR, "head2": ASSERT_FAIL, "fix2": ""}, prover=prover)
    _, verdict = await check(ctx)
    assert verdict.verified
    assert prover.calls[0] is None and "ModuleNotFoundError" in prover.calls[1]


async def test_broken_test_twice_is_downgraded():
    ctx, _ = setup({"head1": IMPORT_ERROR, "head2": IMPORT_ERROR})
    comment, verdict = await check(ctx)
    assert verdict.downgraded and comment.severity == "medium"
    assert "Could not write a working proof test" in verdict.feedback


def test_only_runs_for_provable_bugs_with_a_fix():
    ctx, _ = setup({})
    critic = BugProofCritic()
    assert critic.applies(cc(), ctx)
    assert not critic.applies(cc(evidence_needed="none"), ctx)
    assert not critic.applies(cc(category="security"), ctx)
    assert not critic.applies(cc(suggested_patch=None), ctx)
    ctx.config = RepoConfig(proof_tests=False)  # repos can turn proofs off in .prreview.yml
    assert not critic.applies(cc(), ctx)


def outcome(*tests):
    return ToolOutcome("pytest", "x", 1, 1, False, True, [], list(tests))


def test_classify_tells_assertion_from_crash():
    fail = TestResult(test_id="t", outcome="failed", message="assert -1 == 5")
    crash = TestResult(test_id="t", outcome="failed", message="E   TypeError: add() missing 1 argument")
    no_raise = TestResult(test_id="t", outcome="failed", message="Failed: DID NOT RAISE <class 'ValueError'>")
    assert classify(outcome(fail))[0] == "assert_fail"
    assert classify(outcome(no_raise))[0] == "assert_fail"
    assert classify(outcome(crash))[0] == "error"  # a crash means the TEST is wrong, not that the bug showed
    assert classify(outcome(TestResult(test_id="t", outcome="passed")))[0] == "pass"
    assert classify(outcome())[0] == "error"


def test_import_hint_handles_src_layout():
    assert import_hint("src/billing/discount.py").startswith("import billing.discount")
    assert import_hint("calc.py").startswith("import calc")
