"""Base vs head delta: keep only the problems THIS PR introduced (LLD §10.4)."""

import re
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha1
from typing import Literal

from core.analysis.models import Finding, TestResult, ToolOutcome
from core.github.diffmap import DiffMap

TestChangeKind = Literal["newly_failing", "new_test_failing", "failure_changed"]
FAILING = ("failed", "error")


@dataclass(frozen=True)
class TestChange:
    __test__ = False  # not a pytest test class, despite the name

    kind: TestChangeKind
    test: TestResult  # the head result
    base_message: str | None = None  # base's failure, for "failure_changed"


@dataclass(frozen=True)
class Delta:
    new_findings: list[Finding] = field(default_factory=list)
    test_changes: list[TestChange] = field(default_factory=list)
    fixed_tests: list[str] = field(default_factory=list)
    # tools we could not compare (failed/timed out on a side): we make no claims about them
    unavailable: list[str] = field(default_factory=list)


# ---------- findings ----------

def _normalize(text: str) -> str:
    text = re.sub(r"0x[0-9a-f]+", "0xN", text.strip().lower())  # memory addresses differ per run
    text = re.sub(r"\d+", "N", text)  # line numbers and counts shift when code moves
    return re.sub(r"\s+", " ", text)


def fingerprint(f: Finding, path: str | None = None) -> str:
    """Identity of a finding that survives line shifts. Line number is deliberately left out."""
    return sha1(f"{f.tool}|{f.rule}|{path or f.path}|{_normalize(f.message)}".encode()).hexdigest()


def finding_delta(base: list[Finding], head: list[Finding], diff: DiffMap) -> list[Finding]:
    # Counter, not set: if base has the same warning twice and head three times, one is new
    base_counts = Counter(fingerprint(f) for f in base)
    new = []
    for f in head:
        # A renamed file's findings are compared under its OLD path, as base knows it
        fp = fingerprint(f, path=diff.renamed.get(f.path, f.path))
        if base_counts[fp] > 0:
            base_counts[fp] -= 1
            continue
        # Only lines this PR added: a new finding elsewhere is too indirect to blame confidently
        if f.line in diff.added.get(f.path, frozenset()):
            new.append(f.model_copy(update={"fingerprint": fp}))
    return new


# ---------- tests ----------

def failure_signature(message: str | None) -> str:
    """The essence of a failure: pytest's final `E   SomeError: ...` line, normalized.

    The full traceback quotes source code the PR edited, so comparing it would report
    "changed" for the same old failure; the exception line alone is stable.
    """
    lines = [ln.strip() for ln in (message or "").splitlines() if ln.strip()]
    error_lines = [ln[1:].strip() for ln in lines if ln.startswith("E ")]
    return _normalize(error_lines[-1] if error_lines else (lines[0] if lines else ""))


def _base_id(test_id: str, diff: DiffMap) -> str:
    path, sep, rest = test_id.partition("::")
    return f"{diff.renamed.get(path, path)}{sep}{rest}"


def compare_tests(base: list[TestResult], head: list[TestResult], diff: DiffMap) -> tuple[list[TestChange], list[str]]:
    by_id = {t.test_id: t for t in base}
    changes: list[TestChange] = []
    fixed: list[str] = []
    for t in head:
        before = by_id.get(_base_id(t.test_id, diff))
        if t.outcome in FAILING:
            if before is None:
                changes.append(TestChange("new_test_failing", t))
            elif before.outcome == "passed":
                changes.append(TestChange("newly_failing", t))  # the LLD's rule
            elif before.outcome in FAILING and failure_signature(before.message) != failure_signature(t.message):
                changes.append(TestChange("failure_changed", t, base_message=before.message))
        elif t.outcome == "passed" and before is not None and before.outcome in FAILING:
            fixed.append(t.test_id)
    return changes, fixed


# ---------- both ----------

def compute_delta(base: list[ToolOutcome], head: list[ToolOutcome], diff: DiffMap) -> Delta:
    base_by_tool = {o.tool: o for o in base}
    findings: list[Finding] = []
    changes: list[TestChange] = []
    fixed: list[str] = []
    unavailable: list[str] = []
    for h in head:
        b = base_by_tool.get(h.tool)
        if b is None or not (b.available and h.available):
            # Without both sides we can't tell old problems from new ones, so we claim nothing
            unavailable.append(h.tool)
            continue
        findings += finding_delta(b.findings, h.findings, diff)
        tool_changes, tool_fixed = compare_tests(b.tests, h.tests, diff)
        changes += tool_changes
        fixed += tool_fixed
    return Delta(findings, changes, fixed, unavailable)
