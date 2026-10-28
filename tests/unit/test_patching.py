import pytest

from core.critics.patching import PatchError, normalize_patch

GOOD = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"


def test_well_formed_patch_is_unchanged():
    assert normalize_patch(GOOD, "calc.py") == GOOD


def test_missing_headers_are_added():
    """What DeepSeek actually produced on prreview-demo PR #7."""
    patch = "@@ -4 +4 @@\n-    return a -b-c\n+    return a + b + c\n"
    assert normalize_patch(patch, "calculator.py").startswith("--- a/calculator.py\n+++ b/calculator.py\n@@ -4,1 +4,1 @@")


def test_wrong_counts_are_recounted():
    patch = "--- a/calc.py\n+++ b/calc.py\n@@ -1,5 +1,9 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
    assert "@@ -1,2 +1,2 @@" in normalize_patch(patch, "calc.py")


def test_code_fences_and_lost_blank_context_lines():
    patch = "```diff\n@@ -1,4 +1,4 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n\n def mul(a, b):\n```"
    out = normalize_patch(patch, "calc.py")
    assert "```" not in out
    assert "@@ -1,4 +1,4 @@" in out  # the blank line counts as context again


def test_multiple_hunks_are_kept():
    patch = "@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\n@@ -10,1 +10,1 @@\n-b = 1\n+b = 2\n"
    assert normalize_patch(patch, "x.py").count("@@ -") == 2


@pytest.mark.parametrize("patch, problem", [
    ("--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-a\n+b\n", "must only change calc.py"),
    ("just change minus to plus", "no @@ hunk"),
    ("@@ -1,1 +1,1 @@\n context only\n", "no added or removed lines"),
    ("@@ -1,1 +1,1 @@\n-a\nthis is prose, not a diff\n", "not part of a unified diff"),
])
def test_unusable_patches_explain_why(patch, problem):
    with pytest.raises(PatchError, match=problem):
        normalize_patch(patch, "calc.py")
