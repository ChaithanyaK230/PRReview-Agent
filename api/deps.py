from collections.abc import AsyncIterator

from arq.connections import ArqRedis
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.session import get_sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def get_queue(request: Request) -> ArqRedis:
    # Created once at startup in api/main.py; ArqRedis is also a normal Redis client
    return request.app.state.queue