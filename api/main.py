from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from api import stats, webhooks
from core.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.queue = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    yield
    await app.state.queue.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="PR Review Agent", lifespan=lifespan)
    app.include_router(webhooks.router)
    app.include_router(stats.router)
    return app


app = create_app()