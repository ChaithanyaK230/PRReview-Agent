"""Patch critics against FakeSandbox: what they run, and how they judge the results."""

import json
import uuid

from core.agent.schemas import CandidateComment
from core.analysis.config import RepoConfig
from core.analysis.detect import Toolchain
from core.analysis.models import TestResult
from core.analysis.runners.pytest import OUT_DIR
from core.analysis.workspace import SideSetup, Workspace
from core.critics.base import Attempt, CriticContext, protected_test_ids
from core.critics.patch import PatchApplyCritic, PatchLintCritic, PatchTestsCritic
from core.github.diffmap import DiffMap
from core.sandbox.base import WORK_ROOT, ExecResult
from tests.fakes import FakeSandbox

HEAD = f"{WORK_ROOT}/head"
ATTEMPT = Attempt(uuid.UUID(int=5), 0)
SCRATCH_PREFIX = f"{WORK_ROOT}/scratch/"
PATCH = "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"


def cc(patch=PATCH):
    return CandidateComment(path="calc.py", line=2, category="bug", severity="high", claim="c", body="b",
                            confidence=0.9, suggested_patch=patch)


def workspace():
    side = lambda name: SideSetup(name, f"{WORK_ROOT}/{name}", Toolchain("python", "none"), True, "", 0)  # noqa: E731
    return Workspace(base=side("base"), head=side("head"), tools_ok=True)


def ctx(sbx, head_tests=None, base_tests=None):
    return CriticContext(diff=DiffMap.from_diff(""), config=RepoConfig(), sandbox=sbx, workspace=workspace(),
                         head_tests=head_tests, base_tests=base_tests)


class CwdSandbox(FakeSandbox):
    """FakeSandbox whose answers can depend on whether a command runs in head or in the scratch copy."""

    def __init__(self, head_results=None, scratch_results=None):
        super().__init__()
        self.head_results, self.scratch_results = head_results or {}, scratch_results or {}

    async def exec(self, cmd, cwd, timeout_s, env=None):
        self.calls.append((cmd, cwd))
        table = self.scratch_results if cwd.startswith(SCRATCH_PREFIX) else self.head_results
        joined = " ".join(cmd)
        for prefix, result in table.items():
            if prefix in joined:
                return result
        return ExecResult(0, "", "", 1)


def ok(stdout=""):
    return ExecResult(0, stdout, "", 1)


# ---------- patch_apply ----------

async def test_apply_copies_head_writes_normalized_patch_and_runs_gnu_patch():
    sbx = CwdSandbox()
    comment, verdict = await PatchApplyCritic().check(cc(), ctx(sbx), ATTEMPT)
    assert verdict.passed
    assert comment.suggested_patch.startswith("--- a/calc.py\n+++ b/calc.py\n")  # headers now present
    scratch = ctx(sbx).scratch_dir(ATTEMPT)
    assert sbx.files[f"{scratch}/.prreview-fix.patch"].decode() == comment.suggested_patch
    cmds = [" ".join(c) for c, _ in sbx.calls]
    assert any("--exclude=./.venv" in c for c in cmds)  # copy without the virtualenv
    assert any(c.startswith("patch --batch --forward --fuzz=1") for c in cmds)


async def test_apply_failure_feedback_quotes_patch_output():
    sbx = CwdSandbox(scratch_results={"patch --batch": ExecResult(1, "Hunk #1 FAILED at 1.", "", 1)})
    _, verdict = await PatchApplyCritic().check(cc(), ctx(sbx), ATTEMPT)
    assert not verdict.passed and "Hunk #1 FAILED" in verdict.feedback


async def test_malformed_patch_fails_without_touching_the_sandbox():
    sbx = CwdSandbox()
    _, verdict = await PatchApplyCritic().check(cc(patch="replace minus with plus"), ctx(sbx), ATTEMPT)
    assert not verdict.passed and "unusable" in verdict.feedback and sbx.calls == []


def test_patch_critics_skip_without_sandbox_or_patch():
    no_sandbox = CriticContext(diff=DiffMap.from_diff(""), config=RepoConfig())
    assert not PatchApplyCritic().applies(cc(), no_sandbox)
    assert not PatchApplyCritic().applies(cc(patch=None), ctx(CwdSandbox()))


# ---------- patch_lint ----------

def ruff_json(root, *items):
    return json.dumps([{"code": code, "filename": f"{root}/calc.py", "location": {"row": row, "column": 1},
                        "end_location": {"row": row, "column": 5}, "message": msg} for code, row, msg in items])


async def test_lint_flags_only_new_problems():
    scratch = ctx(CwdSandbox()).scratch_dir(ATTEMPT)
    old = ("E501", 9, "Line too long (100 > 88)")
    sbx = CwdSandbox(head_results={"ruff": ok(ruff_json(HEAD, old))},
                     scratch_results={"ruff": ok(ruff_json(scratch, old, ("F821", 2, "Undefined name `c`")))})
    _, verdict = await PatchLintCritic().check(cc(), ctx(sbx), ATTEMPT)
    assert not verdict.passed
    assert "F821" in verdict.feedback and "E501" not in verdict.feedback  # the old warning isn't the patch's fault


async def test_lint_passes_when_nothing_new():
    sbx = CwdSandbox()  # ruff prints nothing on either side
    _, verdict = await PatchLintCritic().check(cc(), ctx(sbx), ATTEMPT)
    assert verdict.passed


# ---------- patch_tests ----------

def junit(*cases):
    body = "".join(f'<testcase classname="test_calc" name="{name}" file="test_calc.py">{inner}</testcase>'
                   for name, inner in cases)
    return f"<testsuites><testsuite>{body}</testsuite></testsuites>".encode()


def head_passing(*names):
    return [TestResult(test_id=f"test_calc.py::{n}", outcome="passed") for n in names]


async def test_tests_detects_a_regression_caused_by_the_patch():
    sbx = CwdSandbox()
    sbx.files[f"{OUT_DIR}/patch-{ATTEMPT.candidate_id.hex[:12]}-junit.xml"] = junit(
        ("test_add", ""), ("test_mul", '<failure message="x">E   assert 6 == 8</failure>'))
    _, verdict = await PatchTestsCritic().check(cc(), ctx(sbx, head_tests=head_passing("test_add", "test_mul")),
                                                ATTEMPT)
    assert not verdict.passed
    assert "test_calc.py::test_mul" in verdict.feedback and "assert N == N" in verdict.feedback


async def test_tests_pass_and_patched_code_is_put_first_on_the_path():
    sbx = CwdSandbox()
    sbx.files[f"{OUT_DIR}/patch-{ATTEMPT.candidate_id.hex[:12]}-junit.xml"] = junit(("test_add", ""))
    critic_ctx = ctx(sbx, head_tests=head_passing("test_add"))
    _, verdict = await PatchTestsCritic().check(cc(), critic_ctx, ATTEMPT)
    assert verdict.passed and "1 protected tests pass" in verdict.feedback
    pytest_cmd, cwd = next((c, d) for c, d in sbx.calls if "pytest" in c)
    assert pytest_cmd[0] == f"{HEAD}/.venv/bin/python"  # borrows head's interpreter
    assert cwd == critic_ctx.scratch_dir(ATTEMPT)          # but runs the patched copy


async def test_tests_skipped_without_a_baseline():
    sbx = CwdSandbox()
    critic_ctx = CriticContext(diff=DiffMap.from_diff(""), config=RepoConfig(), sandbox=sbx,
                               workspace=Workspace(*(SideSetup(n, f"{WORK_ROOT}/{n}", Toolchain("python", "none"),
                                                               False, "", 0) for n in ("base", "head")), True))
    _, verdict = await PatchTestsCritic().check(cc(), critic_ctx, ATTEMPT)
    assert verdict.passed and verdict.feedback.startswith("skipped")


def result(name, outcome="passed"):
    return TestResult(test_id=f"test_calc.py::{name}", outcome=outcome)


async def test_fix_that_only_makes_head_parse_is_rejected_when_base_disagrees():
    """Live PR #16: head had `a /// b`, so no test loaded; the LLM's fix `a // b` parsed but broke multiply."""
    head = [TestResult(test_id="test_calc.py::<collection>", outcome="error", message="SyntaxError")]
    base = [result("test_add"), result("test_multiply")]
    sbx = CwdSandbox()
    sbx.files[f"{OUT_DIR}/patch-{ATTEMPT.candidate_id.hex[:12]}-junit.xml"] = junit(
        ("test_add", ""), ("test_multiply", '<failure message="x">E   assert 0 == 20</failure>'))
    _, verdict = await PatchTestsCritic().check(cc(), ctx(sbx, head_tests=head, base_tests=base), ATTEMPT)
    assert not verdict.passed
    assert "test_calc.py::test_multiply" in verdict.feedback and "intended behavior" in verdict.feedback


def test_protected_tests_rules():
    head = [result("test_add"), result("test_mul", "failed")]
    # passing on base and still present on head: protected even though head currently fails it
    assert protected_test_ids(head, [result("test_add"), result("test_mul")]) == {
        "test_calc.py::test_add", "test_calc.py::test_mul"}
    # a test the PR deleted is not protected; base results unknown (resumed job) -> head only
    assert protected_test_ids(head, [result("test_old")]) == {"test_calc.py::test_add"}
    assert protected_test_ids(head, None) == {"test_calc.py::test_add"}
    # base tests failing on base protect nothing
    assert protected_test_ids([], [result("test_x", "failed")]) == set()
