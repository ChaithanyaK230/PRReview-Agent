"""EMBEDDED_WORKER (free single-service hosting): the API process also runs the arq worker."""

import asyncio

from fastapi import FastAPI
from redis.asyncio import Redis

from api import main
from core.settings import get_settings
from worker.embedded import embedded_worker

QUEUE = "arq:test-embedded"  # never the real queue: a test worker must not take a developer's jobs
HEALTH_KEY = f"{QUEUE}:health-check"  # arq writes it once the worker is polling


async def test_lifespan_starts_and_stops_the_worker(monkeypatch):
    monkeypatch.setattr(get_settings(), "embedded_worker", True)
    monkeypatch.setattr("worker.embedded.embedded_worker", lambda: embedded_worker(queue_name=QUEUE))
    redis = Redis.from_url(get_settings().redis_url)
    try:
        async with main.lifespan(FastAPI()):
            for _ in range(50):
                if await redis.exists(HEALTH_KEY):
                    break
                await asyncio.sleep(0.1)
            assert await redis.exists(HEALTH_KEY), "embedded worker never started polling"
        assert not await redis.exists(HEALTH_KEY)  # removed on a clean shutdown
    finally:
        await redis.aclose()


async def test_no_worker_by_default(monkeypatch):
    started = []
    monkeypatch.setattr("worker.embedded.embedded_worker", lambda: started.append(1))
    async with main.lifespan(FastAPI()):
        pass
    assert started == []
