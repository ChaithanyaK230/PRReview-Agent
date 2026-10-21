from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.db import repo
from core.db.models import JobTrigger

# pull_request actions that start a review (LLD §5.2)
REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


@dataclass(frozen=True)
class ReviewRequest:
    installation_id: int
    account_login: str
    account_type: str
    repo_id: int
    repo_full_name: str
    private: bool
    pr_number: int
    base_sha: str
    head_sha: str
    trigger: JobTrigger


def parse_review_request(event: str, payload: dict[str, Any], bot_login: str) -> ReviewRequest | None:
    """Return a ReviewRequest if this event should start a review, else None."""
    if event != "pull_request" or payload.get("action") not in REVIEW_ACTIONS:
        return None
    pr = payload["pull_request"]
    if pr.get("draft"):
        return None
    author = pr["user"]
    if author["login"] == bot_login or author.get("type") == "Bot":
        return None  # never review bot PRs (including our own)

    repository = payload["repository"]
    return ReviewRequest(
        installation_id=payload["installation"]["id"],
        account_login=repository["owner"]["login"],
        account_type=repository["owner"]["type"],
        repo_id=repository["id"],
        repo_full_name=repository["full_name"],
        private=repository["private"],
        pr_number=pr["number"],
        base_sha=pr["base"]["sha"],
        head_sha=pr["head"]["sha"],
        trigger=JobTrigger(payload["action"]),
    )


async def apply_installation_event(s: AsyncSession, event: str, payload: dict[str, Any]) -> None:
    """Keep installations/repos in sync with installation and installation_repositories events."""
    inst = payload["installation"]
    inst_id = inst["id"]
    account = inst["account"]
    action = payload["action"]
    await repo.upsert_installation(s, inst_id, account["login"], account["type"])

    if event == "installation":
        if action in ("deleted", "suspend"):
            # Kept (not deleted) so job history stays intact; suspended installs get no reviews
            await repo.set_installation_suspended(s, inst_id, True)
        else:  # created, unsuspend, new_permissions_accepted
            await repo.set_installation_suspended(s, inst_id, False)
            for r in payload.get("repositories") or []:
                await repo.upsert_repo(s, r["id"], inst_id, r["full_name"], r["private"])
    elif event == "installation_repositories":
        for r in payload.get("repositories_added") or []:
            await repo.upsert_repo(s, r["id"], inst_id, r["full_name"], r["private"])
        for r in payload.get("repositories_removed") or []:
            await repo.disable_repo(s, r["id"])