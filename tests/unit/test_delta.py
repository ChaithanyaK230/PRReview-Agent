from core.analysis.delta import compare_tests, compute_delta, failure_signature, finding_delta
from core.analysis.models import Finding, TestResult, ToolOutcome
from core.github.diffmap import DiffMap

# app.py: lines 10-12 added. util.py renamed from old_util.py with line 3 added.
DIFF = DiffMap.from_diff("\n".join([
    "diff --git a/app.py b/app.py",
    "index 1111111..2222222 100644",
    "--- a/app.py",
    "+++ b/app.py",
    "@@ -9,1 +9,4 @@",
    " x = 1",
    "+a = 1",
    "+b = 2",
    "+c = 3",
    "diff --git a/old_util.py b/util.py",
    "similarity index 90%",
    "rename from old_util.py",
    "rename to util.py",
    "index 3333333..4444444 100644",
    "--- a/old_util.py",
    "+++ b/util.py",
    "@@ -1,2 +1,3 @@",
    " def f():",
    "     pass",
    "+def g(): pass",
]) + "\n")


def lint(path, line, rule="F821", message="Undefined name `c`"):
    return Finding(tool="ruff", rule=rule, path=path, line=line, severity="error", message=message)


def result(test_id, outcome, message=None):
    return TestResult(test_id=test_id, outcome=outcome, message=message)


# ---------- findings ----------

def test_preexisting_finding_is_not_blamed_even_if_its_line_moved():
    base = [lint("app.py", 7, message="Line 7 is too long (120 > 88)")]
    head = [lint("app.py", 11, message="Line 11 is too long (120 > 88)")]  # same issue, shifted by the PR
    assert finding_delta(base, head, DIFF) == []


def test_new_finding_on_added_line_is_kept_with_fingerprint():
    [new] = finding_delta([], [lint("app.py", 11)], DIFF)
    assert new.line == 11 and len(new.fingerprint) == 40


def test_new_finding_on_untouched_line_is_not_blamed():
    assert finding_delta([], [lint("app.py", 50)], DIFF) == []


def test_duplicates_are_counted_not_collapsed():
    base = [lint("app.py", 10)]
    head = [lint("app.py", 10), lint("app.py", 11)]  # same message twice; base had it once
    assert len(finding_delta(base, head, DIFF)) == 1


def test_renamed_file_keeps_its_old_findings():
    base = [lint("old_util.py", 3, message="unused variable `z`")]
    head = [lint("util.py", 3, message="unused variable `z`")]
    assert finding_delta(base, head, DIFF) == []


# ---------- tests ----------

def test_newly_failing_test():
    changes, _ = compare_tests([result("t.py::test_a", "passed")],
                               [result("t.py::test_a", "failed", "E   assert 1 == 2")], DIFF)
    assert [c.kind for c in changes] == ["newly_failing"]


def test_new_failing_test_added_by_pr():
    changes, _ = compare_tests([], [result("t.py::test_new", "failed", "E   AssertionError")], DIFF)
    assert [c.kind for c in changes] == ["new_test_failing"]


def test_already_failing_the_same_way_is_not_reported():
    base = [result("t.py::test_a", "failed", "def test_a():\n>  assert add(1, 1) == 3\nE   assert 2 == 3")]
    head = [result("t.py::test_a", "failed", "def test_a():\n>  assert add(1, 1, 0) == 3\nE   assert 2 == 3")]
    # the quoted code changed (PR edited it) but the failure itself is identical
    assert compare_tests(base, head, DIFF) == ([], [])


def test_pr7_collection_error_that_changed_is_reported():
    """Real case from prreview-demo PR #7: base and head both crash at import, differently."""
    base = [result("test_calculator.py::<collection>", "error",
                 "collection failure\nE   TypeError: Maths.multiply() takes 3 positional arguments but 4 were given")]
    head = [result("test_calculator.py::<collection>", "error",
                 "collection failure\nE   TypeError: Maths.add() missing 1 required positional argument: 'c'")]
    changes, _ = compare_tests(base, head, DIFF)
    [change] = changes
    assert change.kind == "failure_changed"
    assert "multiply" in change.base_message


def test_memory_addresses_and_numbers_dont_count_as_changes():
    assert failure_signature("E   ValueError: <obj at 0x7f3a2b> got 3") == failure_signature("E   ValueError: <obj at 0x91cc00> got 4")


def test_fixed_tests_are_listed():
    _, fixed = compare_tests([result("t.py::test_a", "failed", "E   x")], [result("t.py::test_a", "passed")], DIFF)
    assert fixed == ["t.py::test_a"]


def test_renamed_test_file_is_matched_to_its_old_results():
    base = [result("old_util.py::test_x", "passed")]
    head = [result("util.py::test_x", "failed", "E   boom")]
    changes, _ = compare_tests(base, head, DIFF)
    assert [c.kind for c in changes] == ["newly_failing"]


# ---------- whole delta ----------

def outcome(tool, side, available=True, findings=(), tests=()):
    return ToolOutcome(tool, side, 0, 1, False, available, list(findings), list(tests))


def test_unavailable_tool_makes_no_claims():
    base = [outcome("pytest", "base", available=False)]  # e.g. tests timed out on base
    head = [outcome("pytest", "head", tests=[result("t.py::test_a", "failed", "E   x")])]
    delta = compute_delta(base, head, DIFF)
    assert delta.test_changes == [] and delta.unavailable == ["pytest"]


def test_compute_delta_combines_tools():
    base = [outcome("ruff", "base"), outcome("pytest", "base", tests=[result("t.py::test_a", "passed")])]
    head = [outcome("ruff", "head", findings=[lint("app.py", 12)]),
            outcome("pytest", "head", tests=[result("t.py::test_a", "error", "E   ImportError")])]
    delta = compute_delta(base, head, DIFF)
    assert len(delta.new_findings) == 1 and [c.kind for c in delta.test_changes] == ["newly_failing"]
    assert delta.unavailable == []
