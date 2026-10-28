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
from core.analysis.config import CONFIG_PATH, RepoConfig, load_config
from core.analysis.delta import Delta, TestChange, compute_delta
from core.analysis.evidence import Evidence
from core.analysis.models import Finding, TestResult
from core.analysis.runners import Analysis, run_analysis
from core.analysis.workspace import Workspace, prepare_workspace
from core.context.builder import build_context
from core.cost import CostMeter
from core.agent.reviser import Reviser
from core.critics.base import CriticContext
from core.critics.engine import reflect_all
from core.db import models, repo
from core.db.models import TERMINAL_STATES, Base, CandidateStatus, JobState
from core.errors import BudgetExceeded, CancelledJob, FatalError, StateConflict, TransientError
from core.github.client import PullRequest
from core.github.diffmap import DiffMap
from core.github.publisher import (
    SUMMARY_MARKER, ReviewAPI, check_run_title, publish_review, render_summary,
)
from core.ranking import select
from core.sandbox.base import Sandbox
from core.settings import get_settings

log = structlog.get_logger()

# Backoff for TransientError (LLD §7.4); jitter is added on top
RETRY_DELAYS_S = (2, 8, 30)


class GitHubAPI(ReviewAPI, Protocol):
    async def get_pull(self, repo: str, number: int) -> PullRequest: ...
    async def get_diff(self, repo: str, number: int) -> str: ...
    async def get_file(self, repo: str, path: str, ref: str) -> str | None: ...
    async def download_tarball(self, repo: str, ref: str) -> bytes: ...
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
    config: RepoConfig = field(default_factory=RepoConfig)
    # Sandbox (Phase 2). Never persisted: a resumed job builds a new one if it still needs it (LLD §7.2)
    sandbox_factory: Callable[[], Awaitable[Sandbox]] | None = None
    sandbox: Sandbox | None = None
    workspace: Workspace | None = None
    sandbox_note: str = ""  # set when we gave up on the sandbox -> diff-only review
    # Filled by analyze; rebuilt from tool_runs/findings rows if the job resumes later
    evidence: Evidence | None = None
    analysis: Analysis | None = None  # raw tool results; memory only (critics re-run tests if resumed)
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
    rt.config_text = await rt.gh.get_file(rt.repo_full_name, CONFIG_PATH, rt.pr.base_ref)
    rt.config = load_config(rt.config_text)  # invalid config -> FatalError, shown on the Check Run (LLD §7.4)
    log.info("job.fetched", job_id=str(rt.job_id), files=len(rt.diff.right), has_config=rt.config_text is not None)


async def ensure_workspace(rt: JobRuntime) -> None:
    """Sandbox with base + head installed and internet off. Falls back to diff-only instead of failing."""
    if rt.workspace is not None or rt.sandbox_note:
        return
    if rt.sandbox_factory is None:
        rt.sandbox_note = "no sandbox configured"
        return
    await rt.ensure_fetched()
    assert rt.pr is not None
    last_error: Exception | None = None
    for _ in range(2):  # LLD §11: retry sandbox setup once, then review without it
        try:
            base_tar, head_tar = await asyncio.gather(
                rt.gh.download_tarball(rt.repo_full_name, rt.pr.base_sha),
                rt.gh.download_tarball(rt.repo_full_name, rt.head_sha),
            )
            rt.sandbox = await rt.sandbox_factory()
            rt.workspace = await prepare_workspace(rt.sandbox, base_tar, head_tar, rt.config)
            return
        except (TransientError, FatalError) as e:
            last_error = e
            if rt.sandbox is not None:
                await rt.sandbox.close()
                rt.sandbox = None
    rt.sandbox_note = "sandbox unavailable"
    log.warning("job.sandbox_fallback", job_id=str(rt.job_id), error=str(last_error))


async def prepare_sandbox(rt: JobRuntime) -> None:
    await ensure_workspace(rt)


def evidence_rows(job_id: uuid.UUID, analysis: Analysis, delta: Delta) -> list[Base]:
    """tool_runs for every tool on both sides, plus findings rows for what this PR introduced.

    Only new problems are stored (is_new=True), not every pre-existing warning: big repos have thousands.
    A broken test is stored as a pytest finding: rule = change kind, path = test id.
    """
    rows: list[Base] = []
    head_run_ids: dict[str, uuid.UUID] = {}
    for o in [*analysis.base, *analysis.head]:
        run = models.ToolRun(id=uuid.uuid4(), job_id=job_id, tool=o.tool, side=o.side, exit_code=o.exit_code,
                             duration_ms=o.duration_ms, timed_out=o.timed_out, available=o.available)
        rows.append(run)
        if o.side == "head":
            head_run_ids[o.tool] = run.id
    for f in delta.new_findings:
        rows.append(models.Finding(tool_run_id=head_run_ids[f.tool], fingerprint=f.fingerprint, rule=f.rule,
                                   path=f.path, line=f.line, severity=f.severity, message=f.message, is_new=True))
    for c in delta.test_changes:
        rows.append(models.Finding(tool_run_id=head_run_ids["pytest"], fingerprint="", rule=c.kind,
                                   path=c.test.test_id, line=0, severity="error",
                                   message=c.test.message or "", is_new=True))
    return rows


def evidence_from_rows(runs: list[models.ToolRun], findings: list[models.Finding]) -> Evidence:
    """Rebuild Evidence for a job resuming after analyze, without re-running the sandbox."""
    if not runs:
        return Evidence(ran=False, note="sandbox unavailable")
    available = {(r.tool, r.side): r.available for r in runs}
    unavailable = [tool for (tool, side), ok in available.items()
                   if side == "head" and not (ok and available.get((tool, "base"), False))]
    run_tool = {r.id: r.tool for r in runs}
    new_findings, changes = [], []
    for f in findings:
        if run_tool[f.tool_run_id] == "pytest":
            changes.append(TestChange(f.rule, TestResult(test_id=f.path, outcome="failed", message=f.message)))
        else:
            new_findings.append(Finding(tool=run_tool[f.tool_run_id], rule=f.rule, path=f.path, line=f.line,
                                        severity=f.severity, message=f.message, fingerprint=f.fingerprint))
    return Evidence(ran=True, delta=Delta(new_findings, changes, [], unavailable))


async def analyze(rt: JobRuntime) -> None:
    await ensure_workspace(rt)
    if rt.sandbox is None or rt.workspace is None:
        rt.evidence = Evidence(ran=False, note=rt.sandbox_note)
        return
    assert rt.diff is not None
    analysis = await run_analysis(rt.sandbox, rt.workspace, rt.config)
    if not analysis.head:  # nothing could run, e.g. a language we don't support yet
        rt.evidence = Evidence(ran=False, note=rt.workspace.head.log or "no tools ran")
        return
    delta = compute_delta(analysis.base, analysis.head, rt.diff)
    rt.evidence = Evidence(ran=True, delta=delta)
    rt.analysis = analysis  # kept in memory: patch_tests compares against head's passing tests
    rt.pending_writes += evidence_rows(rt.job_id, analysis, delta)
    log.info("job.analyzed", job_id=str(rt.job_id), new_findings=len(delta.new_findings),
             broken_tests=len(delta.test_changes), unavailable=delta.unavailable)


async def draft(rt: JobRuntime) -> None:
    await rt.ensure_fetched()
    assert rt.pr is not None and rt.diff is not None
    context = build_context(rt.pr, rt.diff_text, rt.diff, rt.evidence)
    categories = frozenset(rt.config.categories)
    comments = [] if context.empty else await draft_comments(rt.llm, rt.cost, context.text, categories)
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
    """Critics check every draft; failures go to the reviser; max_iterations rounds (LLD §13.3)."""
    await rt.ensure_fetched()
    assert rt.pr is not None and rt.diff is not None
    pending = [c for c in rt.candidates if c.final_status == CandidateStatus.PENDING]
    if not pending:
        return
    drafts = [(c.id, CandidateComment.model_validate(c.payload)) for c in pending]
    if any(d.suggested_patch for _, d in drafts):
        await ensure_workspace(rt)  # a resumed job may need a fresh sandbox to test patches

    head_tests = None
    if rt.analysis is not None:
        head_pytest = next((o for o in rt.analysis.head if o.tool == "pytest" and o.available), None)
        head_tests = head_pytest.tests if head_pytest else None
    ctx = CriticContext(diff=rt.diff, config=rt.config, sandbox=rt.sandbox, workspace=rt.workspace,
                        head_tests=head_tests)
    reviser = Reviser(rt.llm, rt.cost, build_context(rt.pr, rt.diff_text, rt.diff, rt.evidence).text)
    reflections = await reflect_all(drafts, ctx, reviser, get_settings().max_iterations)

    # All DB writes after every await, so a retried attempt never saves rows twice
    by_id = {c.id: c for c in pending}
    for r in reflections:
        cand = by_id[r.candidate_id]
        for iteration, v in r.verdicts:
            rt.pending_writes.append(models.CriticResult(
                candidate_id=cand.id, iteration=iteration, critic=v.critic,
                passed=v.passed, feedback=v.feedback[:4000], duration_ms=v.duration_ms,
            ))
        cand.payload = r.comment.model_dump()
        cand.history = r.history
        cand.iterations = r.iterations
        cand.patch_verified = r.patch_verified
        cand.verified = r.verified
        if r.status == "dropped":
            cand.final_status = CandidateStatus.DROPPED
        else:
            cand.final_status = CandidateStatus.VERIFIED if r.verified else CandidateStatus.ACCEPTED
        rt.pending_writes.append(cand)
    rt.pending_writes += rt.llm.drain_records()  # reviser calls
    rt.job_fields["cost_usd"] = rt.cost.spent
    log.info("job.reflected", job_id=str(rt.job_id), candidates=len(reflections),
             accepted=sum(r.status == "accepted" for r in reflections),
             revised=sum(r.iterations > 1 for r in reflections),
             patches_verified=sum(r.patch_verified for r in reflections))


async def publish(rt: JobRuntime) -> None:
    settings = get_settings()
    accepted = [
        (c.id, CandidateComment.model_validate(c.payload), c.final_status == CandidateStatus.VERIFIED)
        for c in rt.candidates
        if c.final_status in (CandidateStatus.ACCEPTED, CandidateStatus.VERIFIED)
    ]
    ranked = select(accepted, rt.posted_hashes, rt.config.max_comments)

    result = await publish_review(
        rt.gh, rt.repo_full_name, rt.pr_number, rt.head_sha, ranked, settings.bot_login,
        patch_verified_ids=frozenset(c.id for c in rt.candidates if c.patch_verified),
    )
    evidence = rt.evidence or Evidence(ran=False, note="not recorded")
    summary = render_summary(rt.head_sha, ranked, dropped=len(rt.candidates) - len(ranked),
                             evidence_note=evidence.summary_line())
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
        # Resuming after the analyze step: rebuild its evidence from the DB instead of re-running tools
        resumed_evidence = (
            evidence_from_rows(await repo.get_tool_runs(s, job.id), await repo.get_new_findings(s, job.id))
            if job.state in (JobState.DRAFTED, JobState.REFLECTING, JobState.PUBLISHING) else None
        )

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
        evidence=resumed_evidence,
        sandbox_factory=ctx.get("sandbox_factory"),
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
    finally:
        # Always, on success, failure, cancel or crash: a live sandbox bills per second (LLD §7.3)
        if rt.sandbox is not None:
            await rt.sandbox.close()
