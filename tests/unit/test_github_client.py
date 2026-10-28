import httpx
import pytest
import respx

from core.errors import FatalError, TransientError
from core.github.client import GitHubClient

API = "https://api.github.com"
REPO = "octo/demo"
PULL_URL = f"{API}/repos/{REPO}/pulls/7"


class FakeTokens:
    async def get(self, installation_id):
        return "ghs_test"


PULL_JSON = {
    "number": 7, "title": "Fix totals", "body": None, "draft": False, "state": "open",
    "user": {"login": "alice"},
    "base": {"ref": "main", "sha": "a" * 40},
    "head": {"ref": "fix", "sha": "b" * 40},
}


@pytest.fixture
async def gh():
    async with httpx.AsyncClient(base_url=API) as http:
        yield GitHubClient(http, FakeTokens(), installation_id=1)


@respx.mock
async def test_get_pull_parses_fields(gh):
    route = respx.get(PULL_URL).respond(200, json=PULL_JSON)
    pr = await gh.get_pull(REPO, 7)
    assert (pr.title, pr.body, pr.base_sha, pr.head_sha) == ("Fix totals", "", "a" * 40, "b" * 40)
    assert route.calls.last.request.headers["Authorization"] == "token ghs_test"


@respx.mock
async def test_get_diff_asks_for_diff_format(gh):
    route = respx.get(PULL_URL).respond(200, text="diff --git a/x b/x\n")
    assert (await gh.get_diff(REPO, 7)).startswith("diff --git")
    assert route.calls.last.request.headers["Accept"] == "application/vnd.github.diff"


@respx.mock
async def test_missing_file_returns_none(gh):
    respx.get(f"{API}/repos/{REPO}/contents/.prreview.yml").respond(404)
    assert await gh.get_file(REPO, ".prreview.yml", "main") is None


@respx.mock
async def test_server_error_is_transient(gh):
    respx.get(PULL_URL).respond(502)
    with pytest.raises(TransientError):
        await gh.get_pull(REPO, 7)


@respx.mock
async def test_rate_limit_is_transient(gh):
    respx.get(PULL_URL).respond(403, headers={"retry-after": "30"})
    with pytest.raises(TransientError):
        await gh.get_pull(REPO, 7)


@respx.mock
async def test_missing_pr_is_fatal(gh):
    respx.get(PULL_URL).respond(404)
    with pytest.raises(FatalError):
        await gh.get_pull(REPO, 7)

# ---------- write methods (Step 15) ----------

from core.errors import ReviewRejected  # noqa: E402

REVIEWS_URL = f"{API}/repos/{REPO}/pulls/7/reviews"
COMMENTS_URL = f"{API}/repos/{REPO}/issues/7/comments"


@respx.mock
async def test_review_is_a_comment_pinned_to_the_commit(gh):
    import json

    route = respx.post(REVIEWS_URL).respond(200, json={"id": 11})
    assert await gh.create_review(REPO, 7, "h" * 40, "", [{"path": "a.py", "line": 1, "body": "x"}]) == 11
    sent = json.loads(route.calls.last.request.content)
    assert sent["event"] == "COMMENT" and sent["commit_id"] == "h" * 40


@respx.mock
async def test_invalid_anchor_raises_review_rejected(gh):
    respx.post(REVIEWS_URL).respond(422, json={"message": "Unprocessable Entity"})
    with pytest.raises(ReviewRejected):
        await gh.create_review(REPO, 7, "h" * 40, "", [])


@respx.mock
async def test_upsert_edits_existing_bot_comment(gh):
    respx.get(COMMENTS_URL).respond(200, json=[
        {"id": 1, "user": {"login": "someone"}, "body": "<!-- prreview:summary -->"},  # not the bot
        {"id": 2, "user": {"login": "bot[bot]"}, "body": "old <!-- prreview:summary -->"},
    ])
    patch = respx.patch(f"{API}/repos/{REPO}/issues/comments/2").respond(200, json={"id": 2})
    post = respx.post(COMMENTS_URL)
    assert await gh.upsert_issue_comment(REPO, 7, "<!-- prreview:summary -->", "new", "bot[bot]") == 2
    assert patch.called and not post.called


@respx.mock
async def test_upsert_creates_comment_when_missing(gh):
    respx.get(COMMENTS_URL).respond(200, json=[])
    respx.post(COMMENTS_URL).respond(201, json={"id": 99})
    assert await gh.upsert_issue_comment(REPO, 7, "<!-- prreview:summary -->", "new", "bot[bot]") == 99


async def test_check_run_can_never_fail_a_pr(gh):
    with pytest.raises(ValueError):
        await gh.complete_check_run(REPO, 1, "failure", "t", "s")


@respx.mock
async def test_permission_error_is_fatal(gh):
    respx.get(PULL_URL).respond(403, json={"message": "Resource not accessible by integration"})
    with pytest.raises(FatalError):
        await gh.get_pull(REPO, 7)


# ---------- tarball download (Step 18) ----------

@respx.mock
async def test_tarball_follows_redirect_to_codeload(gh):
    respx.get(f"{API}/repos/{REPO}/tarball/abc").respond(302, headers={"location": "https://codeload.github.com/x.tar.gz"})
    codeload = respx.get("https://codeload.github.com/x.tar.gz").respond(200, content=b"TARBYTES")
    assert await gh.download_tarball(REPO, "abc") == b"TARBYTES"
    assert "authorization" not in codeload.calls.last.request.headers  # token not leaked to another host


@respx.mock
async def test_huge_tarball_is_refused(gh, monkeypatch):
    from core.github import client as client_module

    monkeypatch.setattr(client_module, "MAX_TARBALL_BYTES", 4)
    respx.get(f"{API}/repos/{REPO}/tarball/abc").respond(200, content=b"12345")
    with pytest.raises(FatalError):
        await gh.download_tarball(REPO, "abc")
