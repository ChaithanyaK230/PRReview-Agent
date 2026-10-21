from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core.db import repo
from core.db.models import JobState, JobTrigger, LLMCall, LLMPurpose
from core.errors import StateConflict

HEAD = "b" * 40


async def seed_job(s):
    await repo.upsert_installation(s, 1, "octo", "User")
    await repo.upsert_repo(s, 10, 1, "octo/demo", private=False)
    job, _ = await repo.create_job(s, 10, 1, "a" * 40, HEAD, JobTrigger.OPENED)
    return job


def llm_call(job_id):
    return LLMCall(job_id=job_id, purpose=LLMPurpose.DRAFT, model="test",
                   input_tokens=1, output_tokens=1, cost_usd=Decimal("0.001"))


async def count_llm_calls(s):
    return await s.scalar(select(func.count()).select_from(LLMCall))


def test_transition_rules():
    assert repo.can_transition(JobState.QUEUED, JobState.FETCHING)
    assert not repo.can_transition(JobState.QUEUED, JobState.DRAFTED)        # can't skip steps
    assert not repo.can_transition(JobState.PUBLISHING, JobState.CANCELLED)  # never half-post
    assert repo.can_transition(JobState.PUBLISHING, JobState.FAILED)
    assert not repo.can_transition(JobState.DONE, JobState.FAILED)           # terminal is final


async def test_webhook_delivery_dedupe(session):
    async with session.begin():
        assert await repo.record_webhook_event(session, "d1", "pull_request", "opened")
        assert not await repo.record_webhook_event(session, "d1", "pull_request", "opened")


async def test_create_job_is_idempotent(session):
    async with session.begin():
        await repo.upsert_installation(session, 1, "octo", "User")
        await repo.upsert_repo(session, 10, 1, "octo/demo", private=False)
        j1, created1 = await repo.create_job(session, 10, 1, "a" * 40, HEAD, JobTrigger.OPENED)
        j2, created2 = await repo.create_job(session, 10, 1, "a" * 40, HEAD, JobTrigger.SYNCHRONIZE)
    assert created1 and not created2
    assert j1.id == j2.id
    assert j1.state == JobState.QUEUED


async def test_advance_saves_state_and_outputs_together(session):
    async with session.begin():
        job = await seed_job(session)
    async with session.begin():
        await repo.advance(session, job.id, JobState.QUEUED, JobState.FETCHING, [llm_call(job.id)])
    async with session.begin():
        assert (await repo.get_job(session, job.id)).state == JobState.FETCHING
        assert await count_llm_calls(session) == 1


async def test_advance_from_wrong_state_rolls_back_everything(session):
    async with session.begin():
        job = await seed_job(session)
    job_id = job.id  # rollback expires ORM objects, so keep the plain ID
    with pytest.raises(StateConflict):
        async with session.begin():
            # job is QUEUED, but this worker thinks it is FETCHING
            await repo.advance(session, job_id, JobState.FETCHING, JobState.SANDBOX_READY, [llm_call(job_id)])
    async with session.begin():
        assert (await repo.get_job(session, job_id)).state == JobState.QUEUED
        assert await count_llm_calls(session) == 0  # the step's output was rolled back too


async def test_terminal_state_sets_finished_at(session):
    async with session.begin():
        job = await seed_job(session)
    async with session.begin():
        await repo.fail(session, job.id, JobState.QUEUED, "boom")
    async with session.begin():
        failed = await repo.get_job(session, job.id)
    assert failed.state == JobState.FAILED
    assert failed.error == "boom"
    assert failed.finished_at is not None