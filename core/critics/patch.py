"""Critics that execute a comment's suggested_patch in the sandbox (LLD §13.2, orders 40-60)."""

from collections import Counter

from core.agent.schemas import CandidateComment
from core.analysis.delta import failure_signature, fingerprint
from core.analysis.runners.pytest import run_pytest
from core.analysis.runners.ruff import parse_ruff, ruff_check
from core.analysis.workspace import SideSetup, copy_tree
from core.critics.base import CriticContext, CriticVerdict
from core.critics.patching import PatchError, align_to_file, normalize_patch

PATCH_FILE = ".prreview-fix.patch"
# GNU patch, not git apply: git rejects LLM hunks that only have leading context (verified in E2B).
# --fuzz=1 tolerates sloppy context; a hunk whose code doesn't match the file still fails.
APPLY_CMD = ["patch", "--batch", "--forward", "--fuzz=1", "-p1", "--no-backup-if-mismatch",
             "--reject-file=-", "-i", PATCH_FILE]
MAX_LISTED = 5


class _PatchCritic:
    def applies(self, c: CandidateComment, ctx: CriticContext) -> bool:
        # No sandbox (diff-only review) → we can't check, so the patch stays shown as "not verified"
        return bool(c.suggested_patch) and ctx.can_execute


class PatchApplyCritic(_PatchCritic):
    """Order 40: the patch applies to a fresh copy of head."""

    name = "patch_apply"

    async def check(self, c, ctx, attempt):
        try:
            patch = normalize_patch(c.suggested_patch or "", c.path)
        except PatchError as e:
            return c, CriticVerdict(critic=self.name, passed=False, feedback=f"suggested_patch is unusable: {e}")
        try:
            original = (await ctx.sandbox.read_file(f"{ctx.workspace.head.root}/{c.path}")).decode("utf-8", "replace")
            patch = align_to_file(patch, original)
        except Exception:  # noqa: BLE001 - e.g. a new file: apply the patch as written
            pass
        scratch = ctx.scratch_dir(attempt)
        await copy_tree(ctx.sandbox, ctx.workspace.head.root, scratch)
        await ctx.sandbox.write_file(f"{scratch}/{PATCH_FILE}", patch.encode())
        result = await ctx.sandbox.exec(APPLY_CMD, cwd=scratch, timeout_s=60)
        await ctx.sandbox.exec(["rm", "-f", PATCH_FILE], cwd=scratch, timeout_s=30)
        normalized = c.model_copy(update={"suggested_patch": patch})  # headers fixed: enables one-click suggestions
        if not result.ok:
            output = (result.stdout + result.stderr).strip()[-800:]
            return normalized, CriticVerdict(critic=self.name, passed=False,
                                             feedback=f"The patch does not apply to {c.path}:\n{output}")
        return normalized, CriticVerdict(critic=self.name, passed=True)


class PatchLintCritic(_PatchCritic):
    """Order 50: the patched file has no lint findings that head's version didn't already have."""

    name = "patch_lint"

    async def check(self, c, ctx, attempt):
        before = await ruff_check(ctx.sandbox, ctx.workspace.head.root, [c.path])
        scratch = ctx.scratch_dir(attempt)
        after = await ruff_check(ctx.sandbox, scratch, [c.path])
        if not (before.ok and after.ok):
            return c, CriticVerdict(critic=self.name, passed=True, feedback="skipped: ruff could not run")
        old = Counter(fingerprint(f) for f in parse_ruff(before.stdout, ctx.workspace.head.root))
        introduced = []
        for f in parse_ruff(after.stdout, scratch):
            fp = fingerprint(f)
            if old[fp] > 0:
                old[fp] -= 1
            else:
                introduced.append(f)
        if introduced:
            listed = "\n".join(f"- line {f.line}: {f.rule} {f.message}" for f in introduced[:MAX_LISTED])
            return c, CriticVerdict(critic=self.name, passed=False,
                                    feedback=f"The patch introduces lint problems in {c.path}:\n{listed}")
        return c, CriticVerdict(critic=self.name, passed=True)


class PatchTestsCritic(_PatchCritic):
    """Order 60: with the patch applied, every protected test passes (passing on head, or on base)."""

    name = "patch_tests"

    async def check(self, c, ctx, attempt):
        baseline = await ctx.protected_tests()
        if baseline is None:
            return c, CriticVerdict(critic=self.name, passed=True, feedback="skipped: no test results for head")
        head = ctx.workspace.head
        scratch = ctx.scratch_dir(attempt)
        patched = SideSetup(side=f"patch-{attempt.candidate_id.hex[:12]}", root=scratch, toolchain=head.toolchain,
                            installed=True, log="", duration_ms=0, python_override=head.python)
        # PYTHONPATH puts the patched copy first, even for editable installs pointing at head
        outcome = await run_pytest(ctx.sandbox, patched, ctx.config, env={"PYTHONPATH": f"{scratch}:{scratch}/src"})
        if not outcome.available:
            return c, CriticVerdict(critic=self.name, passed=False,
                                    feedback=f"Tests could not run with the patch applied: {outcome.log[:300]}")
        results = {t.test_id: t for t in outcome.tests}
        # A test that vanished (e.g. its file no longer imports) counts as broken too
        broken = sorted(tid for tid in baseline if results.get(tid) is None or results[tid].outcome != "passed")
        if broken:
            listed = "\n".join(
                f"- {tid}: {failure_signature(results[tid].message) if tid in results else 'no longer runs'}"
                for tid in broken[:MAX_LISTED])
            return c, CriticVerdict(critic=self.name, passed=False,
                                    feedback=f"With the patch, {len(broken)} test(s) fail that pass on base or "
                                             f"head. Base shows the intended behavior:\n{listed}")
        return c, CriticVerdict(critic=self.name, passed=True, feedback=f"{len(baseline)} protected tests pass")
