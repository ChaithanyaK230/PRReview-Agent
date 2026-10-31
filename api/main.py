from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from fastapi.responses import FileResponse

from api import stats, traces, webhooks
from core.settings import get_settings

DASHBOARD = Path(__file__).parent / "static" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.queue = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    async with AsyncExitStack() as stack:
        if settings.embedded_worker:
            from worker.embedded import embedded_worker  # only this mode needs the worker's dependencies

            await stack.enter_async_context(embedded_worker())
        yield
    await app.state.queue.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="PR Review Agent", lifespan=lifespan)
    app.include_router(webhooks.router)
    app.include_router(stats.router)
    app.include_router(traces.router)

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        # One static page that reads the trace API; routing happens client-side (#/jobs/<id>)
        return FileResponse(DASHBOARD)
    return app


app = create_app()