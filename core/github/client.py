from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from core.errors import FatalError, ReviewRejected, TransientError
from core.github.app_auth import GITHUB_HEADERS

CHECK_RUN_NAME = "PR Review"

# Repos bigger than this (compressed) aren't reviewed with a sandbox; protects worker memory
MAX_TARBALL_BYTES = 50 * 1024 * 1024

# Only these conclusions are ever sent: the bot must never block a merge (no "failure")
ALLOWED_CONCLUSIONS = frozenset({"success", "neutral"})


class TokenProvider(Protocol):
    async def get(self, installation_id: int) -> str: ...


class PullRequest(BaseModel):
    number: int
    title: str
    body: str
    author: str
    draft: bool
    state: str
    base_ref: str
    base_sha: str
    head_sha: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PullRequest":
        return cls(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            author=data["user"]["login"],
            draft=data.get("draft", False),
            state=data["state"],
            base_ref=data["base"]["ref"],
            base_sha=data["base"]["sha"],
            head_sha=data["head"]["sha"],
        )


class GitHubClient:
    """Installation-scoped GitHub REST client (LLD §8.2)."""

    def __init__(self, http: httpx.AsyncClient, tokens: TokenProvider, installation_id: int) -> None:
        self.http = http
        self.tokens = tokens
        self.installation_id = installation_id

    async def _request(
        self, method: str, path: str, *, accept: str | None = None,
        json: Any = None, allowed: frozenset[int] = frozenset(), follow_redirects: bool = False,
    ) -> httpx.Response:
        """Send a request; statuses in `allowed` are returned to the caller instead of raising."""
        token = await self.tokens.get(self.installation_id)
        headers = {**GITHUB_HEADERS, "Authorization": f"token {token}"}
        if accept:
            headers["Accept"] = accept
        try:
            # On a redirect to another host (codeload.github.com) httpx drops the Authorization header
            resp = await self.http.request(
                method, path, headers=headers, json=json, follow_redirects=follow_redirects,
            )
        except httpx.TransportError as e:
            raise TransientError(f"{method} {path}: network error: {e}") from e

        status = resp.status_code
        rate_limited = status in (403, 429) and (
            "retry-after" in resp.headers or resp.headers.get("x-ratelimit-remaining") == "0"
        )
        if status >= 500 or rate_limited:
            raise TransientError(f"{method} {path}: GitHub returned {status}")
        if status in allowed:
            return resp
        if status == 404:
            raise FatalError(f"{method} {path}: not found", user_message="The pull request could not be found.")
        if status >= 400:
            raise FatalError(
                f"{method} {path}: GitHub returned {status}: {resp.text[:300]}",
                user_message="GitHub rejected a request from the app.",
            )
        return resp

    # ---------- Read ----------

    async def get_pull(self, repo: str, number: int) -> PullRequest:
        resp = await self._request("GET", f"/repos/{repo}/pulls/{number}")
        return PullRequest.from_api(resp.json())

    async def get_diff(self, repo: str, number: int) -> str:
        resp = await self._request("GET", f"/repos/{repo}/pulls/{number}", accept="application/vnd.github.diff")
        return resp.text

    async def get_file(self, repo: str, path: str, ref: str) -> str | None:
        """File contents at a commit or branch, or None if it doesn't exist."""
        resp = await self._request(
            "GET", f"/repos/{repo}/contents/{path}?ref={ref}",
            accept="application/vnd.github.raw+json", allowed=frozenset({404}),
        )
        return None if resp.status_code == 404 else resp.text

    async def download_tarball(self, repo: str, ref: str) -> bytes:
        """The repo at `ref` as a .tar.gz with one top-level folder (LLD §8.2).

        The worker downloads it and uploads the bytes to the sandbox, so no GitHub token
        ever enters the sandbox (LLD §9.2).
        """
        resp = await self._request("GET", f"/repos/{repo}/tarball/{ref}", follow_redirects=True)
        if len(resp.content) > MAX_TARBALL_BYTES:
            raise FatalError(
                f"tarball for {repo}@{ref[:7]} is {len(resp.content)} bytes",
                user_message="This repository is too large to run in the sandbox.",
            )
        return resp.content

    # ---------- Reviews ----------

    async def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"/repos/{repo}/pulls/{number}/reviews?per_page=100")
        return resp.json()

    async def create_review(
        self, repo: str, number: int, commit_sha: str, body: str, comments: list[dict[str, Any]]
    ) -> int:
        # event=COMMENT: the bot never approves or requests changes (LLD §15.1)
        payload = {"commit_id": commit_sha, "event": "COMMENT", "body": body, "comments": comments}
        resp = await self._request(
            "POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload, allowed=frozenset({422}),
        )
        if resp.status_code == 422:
            raise ReviewRejected(resp.text[:500])
        return resp.json()["id"]

    async def list_review_comments(self, repo: str, number: int, review_id: int) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"/repos/{repo}/pulls/{number}/reviews/{review_id}/comments?per_page=100")
        return resp.json()

    # ---------- Issue comments ----------

    async def upsert_issue_comment(self, repo: str, number: int, marker: str, body: str, bot_login: str) -> int:
        """Edit the bot's comment containing `marker` if it exists, else create it. Returns the comment id."""
        # First page only: the summary is posted on the first review, so it's among the oldest comments
        resp = await self._request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100")
        for existing in resp.json():
            if existing["user"]["login"] == bot_login and marker in (existing.get("body") or ""):
                await self._request("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", json={"body": body})
                return existing["id"]
        resp = await self._request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        return resp.json()["id"]

    # ---------- Check Runs ----------

    async def create_check_run(self, repo: str, head_sha: str, title: str) -> int:
        resp = await self._request("POST", f"/repos/{repo}/check-runs", json={
            "name": CHECK_RUN_NAME, "head_sha": head_sha, "status": "in_progress",
            "output": {"title": title, "summary": title},
        })
        return resp.json()["id"]

    async def complete_check_run(
        self, repo: str, check_run_id: int, conclusion: str, title: str, summary: str
    ) -> None:
        if conclusion not in ALLOWED_CONCLUSIONS:
            raise ValueError(f"conclusion {conclusion!r} could block merges; use success or neutral")
        await self._request("PATCH", f"/repos/{repo}/check-runs/{check_run_id}", json={
            "status": "completed", "conclusion": conclusion, "output": {"title": title, "summary": summary},
        })
