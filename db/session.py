"""
async db connection, sqlalchemy + asyncpg
supabase postgres via DATABASE_URL
"""

import os
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]  # postgresql+asyncpg://...
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

# pool is per process, real ceiling is this times replica count
# hardcoded 10/20 put two replicas at 60 connections, the direct connection limit
# on a small postgres, so the third replica could not connect
# from env so it scales down as replicas scale up
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "10"))
MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "20"))

engine = create_async_engine(
    DATABASE_URL,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_pre_ping=True,  # catch stale connections
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_db():
    """yields a session, commits on exit, rolls back and reraises on error"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
