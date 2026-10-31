import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from core.agent.schemas import CandidateComment
from core.critics.patching import PatchError, normalize_patch
from core.errors import ReviewRejected
from core.ranking import Ranked

log = structlog.get_logger()

SUMMARY_MARKER = "<!-- prreview:summary -->"
SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
CATEGORY_LABEL = {
    "bug": "Bug", "security": "Security", "perf": "Performance", "test_gap": "Test gap", "style": "Style",
}


class ReviewAPI(Protocol):
    async def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]: ...
    async def create_review(self, repo: str, number: int, commit_sha: str, body: str,
                            comments: list[dict[str, Any]]) -> int: ...
    async def list_review_comments(self, repo: str, number: int, review_id: int) -> list[dict[str, Any]]: ...


def candidate_marker(candidate_id: uuid.UUID) -> str:
    """Hidden tag linking a GitHub comment back to its candidate (for feedback and dedupe)."""
    return f"<!-- prreview:c:{candidate_id} -->"


# ---------- Rendering ----------

def suggestion_text(c: CandidateComment) -> str | None:
    """Replacement text for a one-click GitHub suggestion, or None (LLD §15.2).

    Only when the patch is a single hunk on this file that replaces exactly the commented lines;
    anything else could apply to the wrong lines, so it is shown as a plain diff instead.
    """
    if not c.suggested_patch:
        return None
    try:
        # Normalizing adds the ---/+++ headers LLMs often omit (the Phase 1 one-click bug)
        patch = PatchSet.from_string(normalize_patch(c.suggested_patch, c.path))
    except (UnidiffParseError, PatchError):
        return None
    if len(patch) != 1 or len(patch[0]) != 1:
        return None
    pf, hunk = patch[0], patch[0][0]
    if pf.target_file.removeprefix("b/") != c.path:
        return None
    first = c.start_line or c.line
    if hunk.source_start != first or hunk.source_start + hunk.source_length - 1 != c.line:
        return None
    return "".join(line.value for line in hunk if not line.is_removed)


def render_comment(
    c: CandidateComment, candidate_id: uuid.UUID, verified: bool, patch_verified: bool = False,
    proof_test: str | None = None,
) -> str:
    label = f"{'✅ Verified ' if verified else ''}{CATEGORY_LABEL[c.category]} · {c.severity}"
    parts = [f"{SEVERITY_ICON[c.severity]} **{label}**: {c.claim}", c.body]
    suggestion = suggestion_text(c) if patch_verified else None
    if suggestion is not None:
        # One-click "Commit suggestion" only for fixes that applied and kept passing tests green (HLD G3)
        parts.append(f"```suggestion\n{suggestion}```")
        parts.append("<sub>✔ This fix was applied in a sandbox: it passes lint and keeps passing tests green.</sub>")
    elif c.suggested_patch:
        status = "applies cleanly; lint and tests pass" if patch_verified else "not verified"
        parts.append(
            f"<details><summary>Suggested fix ({status})</summary>\n\n"
            f"```diff\n{c.suggested_patch.strip()}\n```\n</details>"
        )
    if verified and proof_test:
        # The evidence behind the badge: anyone can copy it and run it themselves
        parts.append(
            "<details><summary>Proof test (fails on this PR, passes with the fix)</summary>\n\n"
            f"```python\n{proof_test.strip()}\n```\n</details>"
        )
    parts.append(candidate_marker(candidate_id))
    return "\n\n".join(parts)


def review_comment_payload(r: Ranked, patch_verified: bool = False, proof_test: str | None = None) -> dict[str, Any]:
    c = r.comment
    payload: dict[str, Any] = {
        "path": c.path, "line": c.line, "side": c.side,
        "body": render_comment(c, r.candidate_id, r.verified, patch_verified, proof_test),
    }
    if c.start_line is not None:
        payload["start_line"] = c.start_line
        payload["start_side"] = c.side
    return payload


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


DIFF_ONLY_NOTE = "Diff-only review: tests and linters were not run on this push."


def render_summary(
    head_sha: str, posted: list[Ranked], dropped: int, evidence_note: str = DIFF_ONLY_NOTE,
    trace_url: str | None = None,
) -> str:
    """The one summary comment per PR, edited in place on every push (LLD §15.4)."""
    lines = [SUMMARY_MARKER, f"### PR review · `{head_sha[:7]}`"]
    if not posted:
        lines.append("✅ **No issues found** in this push.")
    else:
        verified = sum(r.verified for r in posted)
        noun = "comment" if len(posted) == 1 else "comments"
        lines += [
            f"**{len(posted)} {noun}** ({verified} verified)",
            "",
            "| | Issue | File |",
            "|---|---|---|",
            *(f"| {SEVERITY_ICON[r.comment.severity]} | {_cell(r.comment.claim)} | "
              f"`{r.comment.path}:{r.comment.line}` |" for r in posted),
        ]
    if dropped:
        lines.append(f"\n<sub>{dropped} draft comment(s) were dropped by checks or as duplicates.</sub>")
    lines.append(f"\n<sub>{evidence_note}</sub>")
    if trace_url:
        lines.append(f"\n[View full trace]({trace_url})")
    return "\n".join(lines)


def check_run_title(posted: list[Ranked]) -> tuple[str, str]:
    """(conclusion, title) for the finished Check Run. Never "failure" (LLD §15.5)."""
    if not posted:
        return "success", "No issues found"
    verified = sum(r.verified for r in posted)
    noun = "comment" if len(posted) == 1 else "comments"
    return "neutral", f"{len(posted)} {noun}, {verified} verified"


# ---------- Posting ----------

@dataclass
class PublishResult:
    review_id: int | None
    posted: dict[uuid.UUID, int] = field(default_factory=dict)  # candidate id -> GitHub comment id
    inline: bool = True


async def publish_review(
    gh: ReviewAPI, repo: str, number: int, head_sha: str, ranked: list[Ranked], bot_login: str,
    patch_verified_ids: frozenset[uuid.UUID] = frozenset(), proofs: dict[uuid.UUID, str] | None = None,
) -> PublishResult:
    proofs = proofs or {}
    if not ranked:
        return PublishResult(review_id=None)

    # Idempotent: if a crash happened after posting but before saving, the retry reuses that review
    existing = next(
        (r for r in await gh.list_reviews(repo, number)
         if r["user"]["login"] == bot_login and r.get("commit_id") == head_sha),
        None,
    )
    if existing is not None:
        review_id = existing["id"]
        log.info("publish.reused_review", review_id=review_id)
    else:
        try:
            review_id = await gh.create_review(
                repo, number, head_sha, "",
                [review_comment_payload(r, r.candidate_id in patch_verified_ids, proofs.get(r.candidate_id))
                 for r in ranked],
            )
        except ReviewRejected as e:
            # Fallback: same comments in the review body, so nothing is lost if one anchor is invalid
            log.warning("publish.inline_rejected", error=str(e))
            body = "\n\n---\n\n".join(
                f"`{r.comment.path}:{r.comment.line}`\n\n"
                f"{render_comment(r.comment, r.candidate_id, r.verified, r.candidate_id in patch_verified_ids,
                                  proofs.get(r.candidate_id))}"
                for r in ranked
            )
            review_id = await gh.create_review(repo, number, head_sha, body, [])
            return PublishResult(review_id=review_id, inline=False)

    by_marker = {candidate_marker(r.candidate_id): r.candidate_id for r in ranked}
    posted: dict[uuid.UUID, int] = {}
    for gc in await gh.list_review_comments(repo, number, review_id):
        for marker, cid in by_marker.items():
            if marker in (gc.get("body") or ""):
                posted[cid] = gc["id"]
    return PublishResult(review_id=review_id, posted=posted)
