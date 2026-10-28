import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import (
    TERMINAL_STATES, Base, CandidateComment, Finding, Installation, JobState, JobTrigger, PostedComment,
    Repo, ReviewJob, ToolRun, WebhookEvent,
)
from core.errors import StateConflict

# Forward path through the state machine (LLD §7.1)
_NEXT = {
    JobState.QUEUED: JobState.FETCHING,
    JobState.FETCHING: JobState.SANDBOX_READY,
    JobState.SANDBOX_READY: JobState.ANALYZED,
    JobState.ANALYZED: JobState.DRAFTED,
    JobState.DRAFTED: JobState.REFLECTING,
    JobState.REFLECTING: JobState.PUBLISHING,
    JobState.PUBLISHING: JobState.DONE,
}


def can_transition(src: JobState, dst: JobState) -> bool:
    if src in TERMINAL_STATES:
        return False
    if dst == JobState.FAILED:
        return True  # differs from LLD diagram: the QUEUED step (Check Run creation) can also fail
    if dst == JobState.CANCELLED:
        return src != JobState.PUBLISHING  # never leave a half-posted review
    return _NEXT.get(src) == dst


# ---------- Webhooks ----------

async def record_webhook_event(s: AsyncSession, delivery_id: str, event: str, action: str | None) -> bool:
    """Returns True if this delivery is new, False if GitHub re-sent it."""
    stmt = (
        insert(WebhookEvent)
        .values(delivery_id=delivery_id, event=event, action=action)
        .on_conflict_do_nothing()
        .returning(WebhookEvent.delivery_id)
    )
    return (await s.execute(stmt)).scalar_one_or_none() is not None


# ---------- Installations and repos ----------

async def upsert_installation(s: AsyncSession, installation_id: int, account_login: str, account_type: str) -> None:
    stmt = insert(Installation).values(
        id=installation_id, account_login=account_login, account_type=account_type
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Installation.id],
        set_={"account_login": stmt.excluded.account_login, "account_type": stmt.excluded.account_type},
    )
    await s.execute(stmt)


async def set_installation_suspended(s: AsyncSession, installation_id: int, suspended: bool) -> None:
    await s.execute(update(Installation).where(Installation.id == installation_id).values(suspended=suspended))


async def upsert_repo(s: AsyncSession, repo_id: int, installation_id: int, full_name: str, private: bool) -> None:
    stmt = insert(Repo).values(
        id=repo_id, installation_id=installation_id, full_name=full_name, private=private, enabled=True
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Repo.id],
        set_={"full_name": stmt.excluded.full_name, "private": stmt.excluded.private, "enabled": True},
    )
    await s.execute(stmt)


async def disable_repo(s: AsyncSession, repo_id: int) -> None:
    await s.execute(update(Repo).where(Repo.id == repo_id).values(enabled=False))


# ---------- Review jobs ----------

async def create_job(
    s: AsyncSession, repo_id: int, pr_number: int, base_sha: str, head_sha: str, trigger: JobTrigger
) -> tuple[ReviewJob, bool]:
    """Idempotent on (repo, PR, head SHA). Returns (job, created)."""
    stmt = (
        insert(ReviewJob)
        .values(id=uuid.uuid4(), repo_id=repo_id, pr_number=pr_number,
                base_sha=base_sha, head_sha=head_sha, trigger=trigger)
        .on_conflict_do_nothing(index_elements=["repo_id", "pr_number", "head_sha"])
        .returning(ReviewJob.id)
    )
    created = (await s.execute(stmt)).scalar_one_or_none() is not None
    job = await s.scalar(
        select(ReviewJob).where(
            ReviewJob.repo_id == repo_id, ReviewJob.pr_number == pr_number, ReviewJob.head_sha == head_sha
        )
    )
    assert job is not None
    return job, created


async def get_job(s: AsyncSession, job_id: uuid.UUID) -> ReviewJob | None:
    # populate_existing: always re-read, never trust a stale copy in the session
    return await s.get(ReviewJob, job_id, populate_existing=True)


async def advance(
    s: AsyncSession,
    job_id: uuid.UUID,
    from_state: JobState,
    to_state: JobState,
    pending_writes: Sequence[Base] = (),
    **fields: Any,
) -> None:
    """Save a step's outputs and move the job to its next state, in the caller's transaction."""
    if not can_transition(from_state, to_state):
        raise ValueError(f"illegal transition {from_state} -> {to_state}")
    for obj in pending_writes:
        # merge (not add): inserts new rows and also saves changes to rows loaded in an earlier session
        await s.merge(obj)
    values: dict[str, Any] = {"state": to_state, **fields}
    if to_state in TERMINAL_STATES:
        values["finished_at"] = func.now()
    result = await s.execute(
        update(ReviewJob)
        .where(ReviewJob.id == job_id, ReviewJob.state == from_state)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise StateConflict(f"job {job_id} is not in state {from_state}")


async def fail(s: AsyncSession, job_id: uuid.UUID, from_state: JobState, error: str) -> None:
    await advance(s, job_id, from_state, JobState.FAILED, error=error[:2000])


async def get_repo(s: AsyncSession, repo_id: int) -> Repo | None:
    return await s.get(Repo, repo_id)


# ---------- Candidate comments ----------

async def get_candidates(s: AsyncSession, job_id: uuid.UUID) -> list[CandidateComment]:
    result = await s.scalars(select(CandidateComment).where(CandidateComment.job_id == job_id))
    return list(result.all())


async def get_posted_hashes(s: AsyncSession, repo_id: int, pr_number: int, exclude_job_id: uuid.UUID) -> set[str]:
    """claim_hash of every comment already posted on this PR by earlier jobs (cross-push dedupe)."""
    stmt = (
        select(PostedComment.claim_hash)
        .join(CandidateComment, CandidateComment.id == PostedComment.candidate_id)
        .join(ReviewJob, ReviewJob.id == CandidateComment.job_id)
        # Exclude this job: when resuming at PUBLISHING its own comments must not hide themselves
        .where(ReviewJob.repo_id == repo_id, ReviewJob.pr_number == pr_number, ReviewJob.id != exclude_job_id)
    )
    return set((await s.scalars(stmt)).all())


# ---------- Tool evidence ----------

async def get_tool_runs(s: AsyncSession, job_id: uuid.UUID) -> list[ToolRun]:
    return list((await s.scalars(select(ToolRun).where(ToolRun.job_id == job_id))).all())


async def get_new_findings(s: AsyncSession, job_id: uuid.UUID) -> list[Finding]:
    """Findings this job's PR introduced (is_new), for a job resuming after the analyze step."""
    stmt = (
        select(Finding)
        .join(ToolRun, ToolRun.id == Finding.tool_run_id)
        .where(ToolRun.job_id == job_id, Finding.is_new.is_(True))
    )
    return list((await s.scalars(stmt)).all())
