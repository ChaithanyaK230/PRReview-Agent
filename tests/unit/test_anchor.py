from core.agent.schemas import CandidateComment
from core.critics.anchor import check_anchor
from core.github.diffmap import DiffMap

# app.py new-side lines 10..14 are in the diff
DIFF = "\n".join([
    "diff --git a/app.py b/app.py",
    "index 1111111..2222222 100644",
    "--- a/app.py",
    "+++ b/app.py",
    "@@ -10,4 +10,5 @@",
    " a = 1",
    " b = 2",
    "+c = 3",
    " d = 4",
    " e = 5",
]) + "\n"
DM = DiffMap.from_diff(DIFF)


def cc(line: int, **kw) -> CandidateComment:
    return CandidateComment(path="app.py", line=line, category="bug", severity="low",
                            claim="x", body="y", confidence=0.9, **kw)


def test_line_in_diff_passes_unchanged():
    comment, verdict = check_anchor(cc(12), DM)
    assert verdict.passed and comment.line == 12 and verdict.feedback == ""


def test_nearby_line_is_reanchored():
    comment, verdict = check_anchor(cc(16), DM)
    assert verdict.passed
    assert comment.line == 14
    assert verdict.feedback == "re-anchored 16 -> 14"


def test_far_line_fails_with_helpful_feedback():
    _, verdict = check_anchor(cc(40), DM)
    assert not verdict.passed
    assert "[10, 11, 12, 13, 14]" in verdict.feedback


def test_invalid_start_line_is_dropped():
    comment, verdict = check_anchor(cc(12, start_line=2), DM)
    assert verdict.passed and comment.start_line is None
