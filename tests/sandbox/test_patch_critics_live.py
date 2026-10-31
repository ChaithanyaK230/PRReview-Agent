"""Patch critics against a real E2B sandbox. Run with: uv run pytest -m sandbox"""

import io
import tarfile
import uuid

import pytest

from core.agent.schemas import CandidateComment
from core.analysis.config import RepoConfig
from core.analysis.runners import run_analysis
from core.analysis.workspace import prepare_workspace
from core.critics.base import CriticContext
from core.critics.engine import reflect_one
from core.critics.anchor import AnchorCritic
from core.critics.patch import PatchApplyCritic, PatchLintCritic, PatchTestsCritic
from core.github.diffmap import DiffMap
from core.sandbox.e2b import E2BSandbox
from core.settings import get_settings

pytestmark = pytest.mark.sandbox

CALC = "def add(a, b):\n    return a - b\n\n\ndef multiply(a, b):\n    return a * b\n"
TESTS = ("from calc import add, multiply\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
         "def test_multiply():\n    assert multiply(4, 5) == 20\n")
# The PR under review: it introduced `a - b` on line 2
DIFF = DiffMap.from_diff("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
                         "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a - b\n")

GOOD_FIX = "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
# "fixes" add but also breaks multiply: must be caught by patch_tests
BREAKING_FIX = ("@@ -1,6 +1,6 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n \n \n def multiply(a, b):\n"
                "-    return a * b\n+    return a + b\n")
WRONG_CODE_FIX = "@@ -1,2 +1,2 @@\n def add(x, y):\n-    return x - y\n+    return x + y\n"


def repo_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in (("calc.py", CALC), ("test_calc.py", TESTS)):
            data = text.encode()
            info = tarfile.TarInfo(f"owner-repo-abc/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class NoReviser:
    async def revise(self, comment, failures):
        from core.agent.schemas import ReviseResponse
        return ReviseResponse(action="drop", reason="live test: no LLM")


def comment(patch):
    return CandidateComment(path="calc.py", line=2, category="bug", severity="high", claim="add subtracts",
                            body="b", confidence=0.9, suggested_patch=patch)


async def test_patch_critics_judge_real_patches():
    key = get_settings().e2b_api_key
    if key is None:
        pytest.skip("E2B_API_KEY not set")
    config = RepoConfig()
    async with await E2BSandbox.create(key.get_secret_value(), lifetime_s=600) as sbx:
        tar = repo_tarball()
        ws = await prepare_workspace(sbx, tar, tar, config)
        assert ws.can_run_tests
        analysis = await run_analysis(sbx, ws, config)
        head_tests = next(o for o in analysis.head if o.tool == "pytest").tests
        assert {t.test_id: t.outcome for t in head_tests} == {
            "test_calc.py::test_add": "failed", "test_calc.py::test_multiply": "passed"}

        ctx = CriticContext(diff=DIFF, config=config, sandbox=sbx, workspace=ws, head_tests=head_tests)
        critics = [AnchorCritic(), PatchApplyCritic(), PatchLintCritic(), PatchTestsCritic()]

        async def judge(patch):
            return await reflect_one(uuid.uuid4(), comment(patch), critics, ctx, NoReviser(), 1)

        good = await judge(GOOD_FIX)
        assert good.status == "accepted" and good.patch_verified

        breaking = await judge(BREAKING_FIX)
        assert breaking.status == "dropped"
        failed = breaking.verdicts[-1][1]
        assert failed.critic == "patch_tests" and "test_multiply" in failed.feedback

        wrong = await judge(WRONG_CODE_FIX)
        assert wrong.status == "dropped" and wrong.verdicts[-1][1].critic == "patch_apply"


class ScriptedProver:
    """Stands in for the LLM: returns a fixed proof test."""

    def __init__(self, content):
        self.content = content

    async def write(self, comment, file_text, previous_error=None):
        from core.agent.prover import ProofTest
        return ProofTest(content=self.content)


async def test_bug_proof_verifies_a_real_bug():
    from core.critics.proof import BugProofCritic

    key = get_settings().e2b_api_key
    if key is None:
        pytest.skip("E2B_API_KEY not set")
    config = RepoConfig()
    async with await E2BSandbox.create(key.get_secret_value(), lifetime_s=600) as sbx:
        tar = repo_tarball()
        ws = await prepare_workspace(sbx, tar, tar, config)
        critics = [AnchorCritic(), PatchApplyCritic(), PatchLintCritic(), PatchTestsCritic(), BugProofCritic()]

        def ctx(proof):
            return CriticContext(diff=DIFF, config=config, sandbox=sbx, workspace=ws,
                                 prover=ScriptedProver(proof))

        bug = comment(GOOD_FIX).model_copy(update={"evidence_needed": "repro_test"})
        real_proof = "from calc import add\n\n\ndef test_add_adds():\n    assert add(2, 3) == 5\n"
        verified = await reflect_one(uuid.uuid4(), bug, critics, ctx(real_proof), NoReviser(), 1)
        assert verified.status == "accepted" and verified.verified and verified.patch_verified
        assert verified.proof_test == real_proof

        # a "proof" that doesn't show the bug (it passes on the buggy code) must NOT verify anything
        weak_proof = "from calc import multiply\n\n\ndef test_multiply():\n    assert multiply(2, 3) == 6\n"
        weak = await reflect_one(uuid.uuid4(), bug, critics, ctx(weak_proof), NoReviser(), 1)
        assert weak.status == "accepted" and not weak.verified and weak.downgraded
        assert weak.comment.severity == "medium"
