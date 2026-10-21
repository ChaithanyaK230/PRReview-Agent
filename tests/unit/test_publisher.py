import uuid

from core.agent.schemas import CandidateComment
from core.errors import ReviewRejected
from core.github.publisher import (
    SUMMARY_MARKER, candidate_marker, check_run_title, publish_review, render_comment, render_summary,
)
from core.ranking import select

CID = uuid.UUID(int=1)
BOT = "prreview-dev[bot]"
EXACT_FIX = "\n".join([
    "--- a/calculator.py",
    "+++ b/calculator.py",
    "@@ -4,1 +4,1 @@",
    "-    return a - b",
    "+    return a + b",
]) + "\n"


def cc(**kw):
    base = dict(path="calculator.py", line=4, category="bug", severity="high",
                claim="`add` subtracts instead of adding", body="Use `+`.", confidence=0.9)
    return CandidateComment(**{**base, **kw})


# ---------- rendering ----------

def test_exact_patch_becomes_one_click_suggestion():
    body = render_comment(cc(suggested_patch=EXACT_FIX), CID, verified=False)
    assert "```suggestion\n    return a + b\n```" in body


def test_patch_touching_other_lines_is_shown_as_unverified_diff():
    body = render_comment(cc(line=9, suggested_patch=EXACT_FIX), CID, verified=False)
    assert "```suggestion" not in body
    assert "Suggested fix (not yet verified)" in body


def test_garbage_patch_does_not_crash():
    body = render_comment(cc(suggested_patch="just change it to plus"), CID, verified=False)
    assert "```suggestion" not in body


def test_comment_has_label_claim_and_marker():
    body = render_comment(cc(), CID, verified=True)
    assert body.startswith("🔴 **✅ Verified Bug · high**: `add` subtracts instead of adding")
    assert body.endswith(candidate_marker(CID))


def test_summary_without_issues():
    text = render_summary("abc1234def", [], dropped=0)
    assert text.startswith(SUMMARY_MARKER)
    assert "`abc1234`" in text and "No issues found" in text


def test_summary_table_escapes_pipes():
    ranked = select([(CID, cc(claim="a | b"), False)], set(), cap=7)
    text = render_summary("abc1234", ranked, dropped=2)
    assert "**1 comment** (0 verified)" in text
    assert "| 🔴 | a \\| b | `calculator.py:4` |" in text
    assert "2 draft comment(s) were dropped" in text


def test_check_run_never_fails():
    assert check_run_title([]) == ("success", "No issues found")
    assert check_run_title(select([(CID, cc(), False)], set(), 7)) == ("neutral", "1 comment, 0 verified")


# ---------- posting ----------

class FakeReviews:
    def __init__(self, reject_inline=False, existing=()):
        self.reject_inline = reject_inline
        self.reviews = list(existing)
        self.created = []

    async def list_reviews(self, repo, number):
        return self.reviews

    async def create_review(self, repo, number, commit_sha, body, comments):
        if comments and self.reject_inline:
            raise ReviewRejected("Line could not be resolved")
        self.created.append({"body": body, "comments": comments})
        return 900

    async def list_review_comments(self, repo, number, review_id):
        return [{"id": 5000 + i, "body": c["body"]} for i, c in enumerate(self.created[-1]["comments"])]


async def test_posts_one_review_and_maps_comment_ids():
    gh = FakeReviews()
    ranked = select([(CID, cc(), False)], set(), 7)
    result = await publish_review(gh, "o/r", 1, "h" * 40, ranked, BOT)
    assert result.review_id == 900 and result.inline
    assert result.posted == {CID: 5000}


async def test_rejected_anchor_falls_back_to_body_only_review():
    gh = FakeReviews(reject_inline=True)
    ranked = select([(CID, cc(), False)], set(), 7)
    result = await publish_review(gh, "o/r", 1, "h" * 40, ranked, BOT)
    assert not result.inline
    assert gh.created[0]["comments"] == []
    assert "`calculator.py:4`" in gh.created[0]["body"]


async def test_nothing_to_post_makes_no_review():
    gh = FakeReviews()
    assert (await publish_review(gh, "o/r", 1, "h" * 40, [], BOT)).review_id is None
    assert gh.created == []
