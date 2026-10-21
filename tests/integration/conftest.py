import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.db.models import Base

TEST_DB_URL = os.environ.get(
    # 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first and stalls ~2s per connection
    "TEST_DATABASE_URL", "postgresql+asyncpg://prreview:prreview@127.0.0.1:5434/prreview_test"
)


@pytest.fixture
async def engine():
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)
