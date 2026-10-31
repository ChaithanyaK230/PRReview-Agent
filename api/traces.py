"""Trace API (LLD §16): what the bot drafted, what each critic said, what it cost.

Only repos in DEMO_REPOS are visible; GitHub OAuth for private repos is not built yet.
Anything else is a 404, so the API never confirms that a private job exists.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from core.db import traces
from core.settings import get_settings

router = APIRouter(prefix="/api")


class JobSummary(BaseModel):
    id: uuid.UUID
    repo: str
    pr: int
    head_sha: str
    state: str
    cost_usd: float
    posted: int
    verified: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    duration_s: float | None


class ToolRunOut(BaseModel):
    tool: str
    side: str
    exit_code: int | None
    duration_ms: int
    timed_out: bool
    available: bool


class FindingOut(BaseModel):
    tool: str
    rule: str
    path: str
    line: int
    severity: str
    message: str


class LLMCallOut(BaseModel):
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class JobDetail(JobSummary):
    base_sha: str
    tool_runs: list[ToolRunOut]
    findings: list[FindingOut]
    llm_calls: list[LLMCallOut]


class CriticOut(BaseModel):
    critic: str
    passed: bool
    feedback: str
    duration_ms: int


class IterationOut(BaseModel):
    n: int
    comment: dict[str, Any]
    critics: list[CriticOut]


class CandidateTrace(BaseModel):
    id: uuid.UUID
    final_status: str
    score: float | None
    verified: bool
    patch_verified: bool
    proof_test: str | None
    posted: bool
    comment: dict[str, Any]
    iterations: list[IterationOut]


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="job not found")


@router.get("/jobs", response_model=list[JobSummary])
async def list_jobs(
    repo: str | None = None, pr: int | None = None, limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    return await traces.list_jobs(session, get_settings().demo_repos, repo=repo, pr=pr, limit=limit)


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    detail = await traces.get_job_detail(session, job_id, get_settings().demo_repos)
    if detail is None:
        raise _not_found()
    return detail


@router.get("/jobs/{job_id}/candidates", response_model=list[CandidateTrace])
async def get_candidates(job_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    if not await traces.is_visible(session, job_id, get_settings().demo_repos):
        raise _not_found()
    return await traces.get_candidate_traces(session, job_id)
