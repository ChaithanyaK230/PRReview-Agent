"""bug_proof critic (LLD §13.4, order 70): a test that fails on the PR's code and passes with the fix."""

import posixpath
import re
from typing import Literal

from core.agent.schemas import CandidateComment
from core.analysis.models import ToolOutcome
from core.analysis.runners.pytest import run_pytest
from core.analysis.workspace import SideSetup, copy_tree
from core.critics.base import Attempt, CriticContext, CriticVerdict

# Chosen by us, never by the model, so a generated test can't overwrite real project files
PROOF_PATH = "tests/_prreview_proof/test_prreview_proof.py"
PROOF_TIMEOUT_S = 90  # LLD §17: single critic exec
MAX_PROOF_LINES = 200
LOWER = {"high": "medium", "medium": "low"}

_ASSERTION = re.compile(r"AssertionError|DID NOT RAISE|(^|\n)\s*(E\s+)?assert\s")

Kind = Literal["assert_fail", "pass", "error"]


def classify(outcome: ToolOutcome) -> tuple[Kind, str]:
    """Did the proof test fail by assertion (the bug showed), pass, or not work at all?"""
    if not outcome.available:
        return "error", outcome.log
    if not outcome.tests:
        return "error", "pytest collected no test from the proof file"
    failing = [t for t in outcome.tests if t.outcome in ("failed", "error")]
    if not failing:
        return "pass", ""
    detail = "\n\n".join(t.message or t.test_id for t in failing)
    # A TypeError or ImportError means the TEST is broken, not that the bug was shown
    if all(t.outcome == "failed" and _ASSERTION.search(t.message or "") for t in failing):
        return "assert_fail", detail
    return "error", detail


class BugProofCritic:
    name = "bug_proof"

    def applies(self, c: CandidateComment, ctx: CriticContext) -> bool:
        return (c.category == "bug" and c.evidence_needed == "repro_test" and bool(c.suggested_patch)
                and ctx.prover is not None and ctx.config.proof_tests
                and ctx.can_execute and ctx.workspace.head.installed)

    async def _run(self, ctx: CriticContext, attempt: Attempt, root: str, content: str, tag: str) -> ToolOutcome:
        head = ctx.workspace.head
        await ctx.sandbox.exec(["mkdir", "-p", posixpath.join(root, posixpath.dirname(PROOF_PATH))],
                               cwd="/", timeout_s=30)
        await ctx.sandbox.write_file(posixpath.join(root, PROOF_PATH), content.encode())
        side = SideSetup(side=f"proof-{attempt.candidate_id.hex[:12]}-{attempt.iteration}-{tag}", root=root,
                         toolchain=head.toolchain, installed=True, log="", duration_ms=0, python_override=head.python)
        return await run_pytest(ctx.sandbox, side, ctx.config, env={"PYTHONPATH": f"{root}:{root}/src"},
                                targets=[PROOF_PATH], timeout_s=PROOF_TIMEOUT_S)

    def _downgrade(self, c: CandidateComment, reason: str) -> tuple[CandidateComment, CriticVerdict]:
        if c.severity == "low":
            # Below "low" there is nothing left to downgrade to (LLD §13.4): fail, so the reviser drops or fixes it
            return c, CriticVerdict(critic=self.name, passed=False,
                                    feedback=f"{reason} The claim may be wrong: drop it unless you are certain.")
        lowered = c.model_copy(update={"severity": LOWER[c.severity]})
        return lowered, CriticVerdict(critic=self.name, passed=True, downgraded=True,
                                      feedback=f"downgraded {c.severity} -> {lowered.severity}: {reason}")

    async def check(self, c: CandidateComment, ctx: CriticContext, attempt: Attempt):
        head_file = await ctx.sandbox.read_file(posixpath.join(ctx.workspace.head.root, c.path))
        patched_dir = ctx.scratch_dir(attempt)  # patch_apply already applied the fix here
        unpatched_dir = f"{patched_dir}-proof"
        await copy_tree(ctx.sandbox, ctx.workspace.head.root, unpatched_dir)

        error: str | None = None
        for try_no in (1, 2):  # LLD §13.4: a broken proof test gets one retry with pytest's error
            proof = await ctx.prover.write(c, head_file.decode("utf-8", errors="replace"), previous_error=error)
            if len(proof.content.splitlines()) > MAX_PROOF_LINES:
                error = f"the test was {len(proof.content.splitlines())} lines; keep it under 60"
                continue
            kind, detail = classify(await self._run(ctx, attempt, unpatched_dir, proof.content, f"head{try_no}"))
            if kind == "error":
                error = detail[-1500:]
                continue
            if kind == "pass":
                return self._downgrade(c, "Could not reproduce: the proof test passes on the PR's code.")
            fixed_kind, fixed_detail = classify(
                await self._run(ctx, attempt, patched_dir, proof.content, f"fix{try_no}"))
            if fixed_kind == "pass":
                return c, CriticVerdict(critic=self.name, passed=True, verified=True, proof_test=proof.content,
                                        feedback="bug reproduced; the suggested fix makes the proof test pass")
            return c, CriticVerdict(critic=self.name, passed=False, feedback=(
                "The proof test shows the bug, but it still fails with suggested_patch applied, "
                f"so the patch does not fix it:\n{fixed_detail[-1000:]}"))
        return self._downgrade(c, f"Could not write a working proof test: {(error or '')[:300]}")
