"""Read-only queries behind the trace API and dashboard (LLD §16). Returns plain dicts, never ORM objects."""

import uuid
from collections import defaultdict
from statistics import quantiles
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import (
    CandidateComment,
    CandidateStatus,
    CriticResult,
    Finding,
    JobState,
    LLMCall,
    PostedComment,
    Repo,
    ReviewJob,
    ToolRun,
)


def _duration_s(job: ReviewJob) -> float | None:
    if job.finished_at is None:
        return None
    return round((job.finished_at - job.created_at).total_seconds(), 1)


def _summary(job: ReviewJob, repo: str, posted: int, verified: int) -> dict[str, Any]:
    return {
        "id": job.id, "repo": repo, "pr": job.pr_number, "head_sha": job.head_sha, "state": job.state,
        "cost_usd": float(job.cost_usd), "posted": posted, "verified": verified, "error": job.error,
        "created_at": job.created_at, "finished_at": job.finished_at, "duration_s": _duration_s(job),
    }


def _counts():
    """(job_id, posted, verified) per job, as a subquery."""
    return (
        select(
            CandidateComment.job_id,
            func.count(PostedComment.id).label("posted"),
            func.count(PostedComment.id).filter(CandidateComment.verified.is_(True)).label("verified"),
        )
        .join(PostedComment, PostedComment.candidate_id == CandidateComment.id)
        .group_by(CandidateComment.job_id)
        .subquery()
    )


async def list_jobs(
    s: AsyncSession, visible_repos: list[str], repo: str | None = None, pr: int | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    counts = _counts()
    stmt = (
        select(ReviewJob, Repo.full_name, counts.c.posted, counts.c.verified)
        .join(Repo, Repo.id == ReviewJob.repo_id)
        .outerjoin(counts, counts.c.job_id == ReviewJob.id)
        .where(Repo.full_name.in_(visible_repos))
        .order_by(ReviewJob.created_at.desc())
        .limit(limit)
    )
    if repo:
        stmt = stmt.where(Repo.full_name == repo)
    if pr is not None:
        stmt = stmt.where(ReviewJob.pr_number == pr)
    rows = (await s.execute(stmt)).all()
    return [_summary(job, name, posted or 0, verified or 0) for job, name, posted, verified in rows]


async def get_job_detail(s: AsyncSession, job_id: uuid.UUID, visible_repos: list[str]) -> dict[str, Any] | None:
    found = await _find_job(s, job_id, visible_repos)
    if found is None:
        return None
    job, summary = found
    runs = list((await s.scalars(
        select(ToolRun).where(ToolRun.job_id == job_id).order_by(ToolRun.tool, ToolRun.side))).all())
    findings = (await s.execute(
        select(Finding, ToolRun.tool).join(ToolRun, ToolRun.id == Finding.tool_run_id)
        .where(ToolRun.job_id == job_id, Finding.is_new.is_(True)))).all()
    calls = list((await s.scalars(select(LLMCall).where(LLMCall.job_id == job_id))).all())
    return {
        **summary,
        "base_sha": job.base_sha,
        "tool_runs": [
            {"tool": r.tool, "side": r.side, "exit_code": r.exit_code, "duration_ms": r.duration_ms,
             "timed_out": r.timed_out, "available": r.available}
            for r in runs
        ],
        "findings": [
            {"tool": tool, "rule": f.rule, "path": f.path, "line": f.line, "severity": f.severity,
             "message": f.message}
            for f, tool in findings
        ],
        "llm_calls": [
            {"purpose": c.purpose, "model": c.model, "input_tokens": c.input_tokens,
             "output_tokens": c.output_tokens, "cost_usd": float(c.cost_usd)}
            for c in calls
        ],
    }


async def _find_job(
    s: AsyncSession, job_id: uuid.UUID, visible_repos: list[str],
) -> tuple[ReviewJob, dict[str, Any]] | None:
    counts = _counts()
    row = (await s.execute(
        select(ReviewJob, Repo.full_name, counts.c.posted, counts.c.verified)
        .join(Repo, Repo.id == ReviewJob.repo_id)
        .outerjoin(counts, counts.c.job_id == ReviewJob.id)
        .where(ReviewJob.id == job_id, Repo.full_name.in_(visible_repos))
    )).first()
    if row is None:
        return None
    job, name, posted, verified = row
    return job, _summary(job, name, posted or 0, verified or 0)


async def is_visible(s: AsyncSession, job_id: uuid.UUID, visible_repos: list[str]) -> bool:
    return await _find_job(s, job_id, visible_repos) is not None


async def get_candidate_traces(s: AsyncSession, job_id: uuid.UUID) -> list[dict[str, Any]]:
    """LLD §16.1: each candidate's versions, with the critic verdicts each version got.

    The reflection loop appends the old version to `history` before every revision (and before the
    salvage round), so version k of a comment is exactly what the critics saw in iteration k.
    """
    cands = list((await s.scalars(
        select(CandidateComment).where(CandidateComment.job_id == job_id)
        .order_by(CandidateComment.score.desc().nulls_last()))).all())
    if not cands:
        return []
    ids = [c.id for c in cands]
    verdicts: dict[uuid.UUID, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in (await s.scalars(
        select(CriticResult).where(CriticResult.candidate_id.in_(ids)))).all():
        verdicts[r.candidate_id][r.iteration].append(
            {"critic": r.critic, "passed": r.passed, "feedback": r.feedback, "duration_ms": r.duration_ms})
    for per_iter in verdicts.values():  # rows carry no insert order: restore the pipeline order
        for chain in per_iter.values():
            chain.sort(key=lambda v: _CRITIC_ORDER.get(v["critic"], 99))
    posted = set((await s.scalars(select(PostedComment.candidate_id).where(PostedComment.candidate_id.in_(ids)))).all())

    out = []
    for c in cands:
        versions = [*c.history, c.payload]
        out.append({
            "id": c.id, "final_status": c.final_status, "score": c.score, "verified": c.verified,
            "patch_verified": c.patch_verified, "proof_test": c.proof_test, "posted": c.id in posted,
            "comment": c.payload,
            "iterations": [
                {"n": n, "comment": v, "critics": verdicts[c.id].get(n, [])} for n, v in enumerate(versions)
            ],
        })
    return out


# LLD §13.2 order; the engine runs critics in this order, so the dashboard shows them the same way
_CRITIC_ORDER = {"anchor": 10, "grounding": 20, "duplicate_finding": 30, "patch_apply": 40,
                 "patch_lint": 50, "patch_tests": 60, "bug_proof": 70}


async def get_stats(s: AsyncSession, visible_repos: list[str]) -> dict[str, Any]:
    jobs = list((await s.scalars(
        select(ReviewJob).join(Repo, Repo.id == ReviewJob.repo_id).where(Repo.full_name.in_(visible_repos)))).all())
    by_state: dict[str, int] = defaultdict(int)
    for j in jobs:
        by_state[j.state] += 1
    done = [j for j in jobs if j.state == JobState.DONE]
    durations = sorted(d for j in done if (d := _duration_s(j)) is not None)

    counts = (await s.execute(
        select(
            func.count(PostedComment.id),
            func.count(PostedComment.id).filter(CandidateComment.final_status == CandidateStatus.VERIFIED),
            func.count(PostedComment.id).filter(CandidateComment.payload["category"].astext == "bug"),
        )
        .join(CandidateComment, CandidateComment.id == PostedComment.candidate_id)
        .join(ReviewJob, ReviewJob.id == CandidateComment.job_id)
        .join(Repo, Repo.id == ReviewJob.repo_id)
        .where(Repo.full_name.in_(visible_repos))
    )).one()
    posted, verified, bugs = counts
    return {
        "jobs": len(jobs),
        "by_state": dict(by_state),
        "comments_posted": posted,
        "verified": verified,
        "verified_rate": round(verified / bugs, 3) if bugs else None,  # of posted bug comments, as in the evals
        "mean_cost_usd": round(sum(float(j.cost_usd) for j in done) / len(done), 5) if done else None,
        "p50_s": _pct(durations, 50),
        "p95_s": _pct(durations, 95),
    }


def _pct(values: list[float], p: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return round(quantiles(values, n=100, method="inclusive")[p - 1], 1)
