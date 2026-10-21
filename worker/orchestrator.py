import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from core.agent.drafter import draft_comments
from core.agent.schemas import CandidateComment
from core.context.builder import build_context
from core.cost import CostMeter
from core.critics.anchor import check_anchor
from core.db import models, repo
from core.db.models import TERMINAL_STATES, Base, CandidateStatus, JobState
from core.errors import BudgetExceeded, CancelledJob, FatalError, StateConflict, TransientError
from core.github.client import PullRequest
from core.github.diffmap import DiffMap
from core.github.publisher import (
    SUMMARY_MARKER, ReviewAPI, check_run_title, publish_review, render_summary,
)
from core.ranking import select
from core.settings import get_settings

log = structlog.get_logger()

# Backoff for TransientError (LLD §7.4); jitter is added on top
RETRY_DELAYS_S = (2, 8, 30)


class GitHubAPI(ReviewAPI, Protocol):
    async def get_pull(self, repo: str, number: int) -> PullRequest: ...
    async def get_diff(self, repo: str, number: int) -> str: ...
    async def get_file(self, repo: str, path: str, ref: str) -> str | None: ...
    async def upsert_issue_comment(self, repo: str, number: int, marker: str, body: str, bot_login: str) -> int: ...
    async def create_check_run(self, repo: str, head_sha: str, title: str) -> int: ...
    async def complete_check_run(self, repo: str, check_run_id: int, conclusion: str, title: str,
                                 summary: str) -> None: ...


@dataclass
class JobRuntime:
    """Everything one job needs while it runs. Lives only in the worker's memory."""

    job_id: uuid.UUID
    state: JobState
    repo_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str
    gh: GitHubAPI
    llm: Any  # core.agent.llm.LLM (or a fake in tests)
    cost: CostMeter
    check_run_id: int | None = None
    # claim hashes already posted on this PR by earlier pushes; never posted twice
    posted_hashes: set[str] = field(default_factory=set)
    # Written to the DB together with the next state transition
    pending_writes: list[Base] = field(default_factory=list)
    job_fields: dict[str, Any] = field(default_factory=dict)
    # Filled by fetch; re-fetched if the job resumes after a crash
    pr: PullRequest | None = None
    diff_text: str = ""
    diff: DiffMap | None = None
    config_text: str | None = None
    # Filled by draft; loaded from the DB if the job resumes at REFLECTING or later
    candidates: list[models.CandidateComment] = field(default_factory=list)

    async def ensure_fetched(self) -> None:
        if self.pr is None:
            await fetch(self)


# ---------- Step handlers (LLD §7.2) ----------

async def start(rt: JobRuntime) -> None:
    if rt.check_run_id is None:
        rt.check_run_id = await rt.gh.create_check_run(rt.repo_full_name, rt.head_sha, "Reviewing…")
        rt.job_fields["check_run_id"] = rt.check_run_id


async def fetch(rt: JobRuntime) -> None:
    rt.pr = await rt.gh.get_pull(rt.repo_full_name, rt.pr_number)
    if rt.pr.head_sha != rt.head_sha:
        raise CancelledJob(f"PR head moved to {rt.pr.head_sha[:7]}")
    rt.diff_text = await rt.gh.get_diff(rt.repo_full_name, rt.pr_number)
    rt.diff = DiffMap.from_diff(rt.diff_text)
    # Config always from the BASE branch, so a PR can't weaken its own review (LLD §10.1)
    rt.config_text = await rt.gh.get_file(rt.repo_full_name, ".prreview.yml", rt.pr.base_ref)
    log.info("job.fetched", job_id=str(rt.job_id), files=len(rt.diff.right), has_config=rt.config_text is not None)


async def prepare_sandbox(rt: JobRuntime) -> None:
    pass  # Phase 2: E2B sandbox


async def analyze(rt: JobRuntime) -> None:
    pass  # Phase 2: lint/tests on base and head


async def draft(rt: JobRuntime) -> None:
    await rt.ensure_fetched()
    assert rt.pr is not None and rt.diff is not None
    context = build_context(rt.pr, rt.diff_text, rt.diff)
    # TODO: categories from .prreview.yml once RepoConfig exists (LLD §10.1)
    comments = [] if context.empty else await draft_comments(rt.llm, rt.cost, context.text)
    rt.pending_writes += rt.llm.drain_records()
    rt.candidates = [
        models.CandidateComment(
            id=uuid.uuid4(), job_id=rt.job_id, payload=c.model_dump(), final_status=CandidateStatus.PENDING,
        )
        for c in comments
    ]
    rt.pending_writes += rt.candidates
    rt.job_fields["cost_usd"] = rt.cost.spent


async def reflect(rt: JobRuntime) -> None:
    # Phase 1 runs only the anchor critic; the full critic chain and reviser come in Phase 3 (LLD §13)
    await rt.ensure_fetched()
    assert rt.diff is not None
    for cand in rt.candidates:
        if cand.final_status != CandidateStatus.PENDING:
            continue
        comment, verdict = check_anchor(CandidateComment.model_validate(cand.payload), rt.diff)
        rt.pending_writes.append(models.CriticResult(
            candidate_id=cand.id, iteration=0, critic=verdict.critic,
            passed=verdict.passed, feedback=verdict.feedback, duration_ms=verdict.duration_ms,
        ))
        cand.payload = comment.model_dump()
        cand.iterations = 1
        cand.final_status = CandidateStatus.ACCEPTED if verdict.passed else CandidateStatus.DROPPED
        rt.pending_writes.append(cand)
    accepted = sum(c.final_status == CandidateStatus.ACCEPTED for c in rt.candidates)
    log.info("job.reflected", job_id=str(rt.job_id), candidates=len(rt.candidates), accepted=accepted)


async def publish(rt: JobRuntime) -> None:
    settings = get_settings()
    accepted = [
        (c.id, CandidateComment.model_validate(c.payload), c.final_status == CandidateStatus.VERIFIED)
        for c in rt.candidates
        if c.final_status in (CandidateStatus.ACCEPTED, CandidateStatus.VERIFIED)
    ]
    ranked = select(accepted, rt.posted_hashes, settings.max_comments)

    result = await publish_review(
        rt.gh, rt.repo_full_name, rt.pr_number, rt.head_sha, ranked, settings.bot_login
    )
    summary = render_summary(rt.head_sha, ranked, dropped=len(rt.candidates) - len(ranked))
    summary_id = await rt.gh.upsert_issue_comment(
        rt.repo_full_name, rt.pr_number, SUMMARY_MARKER, summary, settings.bot_login
    )
    if rt.check_run_id is not None:
        conclusion, title = check_run_title(ranked)
        await rt.gh.complete_check_run(rt.repo_full_name, rt.check_run_id, conclusion, title, summary)

    # Recorded only after every GitHub call succeeded, so a retried attempt never saves rows twice
    rt.job_fields["summary_comment_id"] = summary_id
    scores = {r.candidate_id: r.score for r in ranked}
    for cand in rt.candidates:
        if cand.id in scores:
            cand.score = scores[cand.id]
            rt.pending_writes.append(cand)
    for r in ranked:
        if r.candidate_id in result.posted:
            rt.pending_writes.append(models.PostedComment(
                candidate_id=r.candidate_id, github_comment_id=result.posted[r.candidate_id],
                review_id=result.review_id, commit_sha=rt.head_sha, claim_hash=r.claim_hash,
            ))
    log.info("job.published", job_id=str(rt.job_id), comments=len(ranked), inline=result.inline)


Handler = Callable[[JobRuntime], Awaitable[None]]

# state on entry -> (handler to run, state after it succeeds)
STEPS: dict[JobState, tuple[Handler, JobState]] = {
    JobState.QUEUED: (start, JobState.FETCHING),
    JobState.FETCHING: (fetch, JobState.SANDBOX_READY),
    JobState.SANDBOX_READY: (prepare_sandbox, JobState.ANALYZED),
    JobState.ANALYZED: (analyze, JobState.DRAFTED),
    JobState.DRAFTED: (draft, JobState.REFLECTING),
    JobState.REFLECTING: (reflect, JobState.PUBLISHING),
    JobState.PUBLISHING: (publish, JobState.DONE),
}


# ---------- Run loop (LLD §7.3) ----------

async def with_retries(handler: Handler, rt: JobRuntime) -> None:
    for attempt, delay in enumerate((*RETRY_DELAYS_S, None), start=1):
        try:
            return await handler(rt)
        except TransientError as e:
            if delay is None:
                raise FatalError(
                    f"{handler.__name__}: still failing after {attempt} attempts: {e}",
                    user_message="GitHub or the AI service kept failing. Push again to retry.",
                ) from e
            log.warning("step.retry", step=handler.__name__, attempt=attempt, error=str(e))
            await asyncio.sleep(delay + random.uniform(0, delay / 2))


async def close_check_run(rt: JobRuntime, title: str, summary: str) -> None:
    """Finish the Check Run as neutral when a job stops early. Best effort: never raises."""
    if rt.check_run_id is None:
        return
    try:
        await rt.gh.complete_check_run(rt.repo_full_name, rt.check_run_id, "neutral", title, summary)
    except Exception as e:  # the job outcome is already saved; a stale check is cosmetic
        log.warning("checks.complete_failed", job_id=str(rt.job_id), error=str(e))


async def ensure_not_superseded(redis: Any, rt: JobRuntime) -> None:
    latest = await redis.get(f"pr:{rt.repo_id}:{rt.pr_number}:latest")
    if isinstance(latest, bytes):
        latest = latest.decode()
    if latest and latest != rt.head_sha:
        raise CancelledJob(f"superseded by newer push {latest[:7]}")


async def run_review(ctx: dict, job_id: str) -> None:
    sessionmaker = ctx["sessionmaker"]
    async with sessionmaker() as s:
        job = await repo.get_job(s, uuid.UUID(job_id))
        if job is None or job.state in TERMINAL_STATES:
            return
        repo_row = await repo.get_repo(s, job.repo_id)
        assert repo_row is not None
        # Resuming after the draft step: reload the candidates it saved
        resumed_candidates = (
            await repo.get_candidates(s, job.id)
            if job.state in (JobState.REFLECTING, JobState.PUBLISHING) else []
        )
        posted_hashes = await repo.get_posted_hashes(s, job.repo_id, job.pr_number, exclude_job_id=job.id)

    cost = CostMeter(get_settings().job_budget_usd)
    cost.add(job.cost_usd)  # spend from before a resume still counts toward the cap
    rt = JobRuntime(
        job_id=job.id, state=job.state, repo_id=job.repo_id, repo_full_name=repo_row.full_name,
        pr_number=job.pr_number, head_sha=job.head_sha,
        gh=ctx["gh_for"](repo_row.installation_id),
        llm=ctx["llm_for"](job.id),
        cost=cost,
        check_run_id=job.check_run_id,
        posted_hashes=posted_hashes,
        candidates=resumed_candidates,
    )
    log.info("job.run", job_id=job_id, repo=rt.repo_full_name, pr=rt.pr_number, resume_from=rt.state)

    try:
        while rt.state not in TERMINAL_STATES:
            if rt.state != JobState.PUBLISHING:  # publishing is never cancelled (no half-posted reviews)
                await ensure_not_superseded(ctx["redis"], rt)
            handler, next_state = STEPS[rt.state]
            started = time.perf_counter()
            await with_retries(handler, rt)
            async with sessionmaker.begin() as s:
                await repo.advance(s, rt.job_id, rt.state, next_state, rt.pending_writes, **rt.job_fields)
            log.info("job.advanced", job_id=job_id, step=handler.__name__, to=next_state,
                     ms=int((time.perf_counter() - started) * 1000))
            rt.state, rt.pending_writes, rt.job_fields = next_state, [], {}
    except CancelledJob as e:
        async with sessionmaker.begin() as s:
            await repo.advance(s, rt.job_id, rt.state, JobState.CANCELLED, error=str(e))
        log.info("job.cancelled", job_id=job_id, reason=str(e))
        await close_check_run(rt, "Superseded by a newer push", str(e))
    except BudgetExceeded as e:
        # TODO (Phase 3): publish the comments accepted so far instead (LLD §7.4)
        async with sessionmaker.begin() as s:
            await repo.fail(s, rt.job_id, rt.state, f"budget exceeded: {e}")
        log.warning("job.budget_exceeded", job_id=job_id)
        await close_check_run(rt, "Review stopped: AI budget reached", "This push used up its review budget.")
    except FatalError as e:
        async with sessionmaker.begin() as s:
            await repo.fail(s, rt.job_id, rt.state, str(e))
        log.error("job.failed", job_id=job_id, state=rt.state, error=str(e))
        await close_check_run(rt, "Review could not complete", e.user_message)
    except StateConflict:
        log.warning("job.conflict", job_id=job_id)  # another worker already moved this job
