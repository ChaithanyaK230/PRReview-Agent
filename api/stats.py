from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_queue, get_session
from core.db import traces
from core.settings import get_settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    session: AsyncSession = Depends(get_session), queue: ArqRedis = Depends(get_queue),
) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    await queue.ping()
    return {"status": "ready"}


@router.get("/api/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await traces.get_stats(session, get_settings().demo_repos)
