"""Run the arq worker inside the API process, for hosts with one free web service (Render free tier).

Same WorkerSettings, startup and shutdown as `arq worker.main.WorkerSettings`; only the process differs.
A review interrupted by a redeploy is not lost: arq retries the job and it resumes from its saved state.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from arq.worker import create_worker

from worker.main import WorkerSettings

log = structlog.get_logger()


@asynccontextmanager
async def embedded_worker(**overrides: Any) -> AsyncIterator[None]:
    """overrides go to arq's Worker, e.g. queue_name in tests so they never take real jobs."""
    worker = create_worker(WorkerSettings, **overrides)
    task = asyncio.create_task(worker.async_run())
    log.info("worker.embedded.started")
    try:
        yield
    finally:
        # Not worker.close(): it signals itself with SIGUSR1, which Windows doesn't have
        task.cancel()
        jobs = list(worker.tasks.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(task, *jobs, return_exceptions=True)
        if worker._pool is not None:  # None if startup never got as far as connecting
            await worker._pool.delete(worker.health_check_key)
            if worker.on_shutdown:
                await worker.on_shutdown(worker.ctx)
            await worker._pool.aclose()
        log.info("worker.embedded.stopped", interrupted_jobs=len(jobs))
