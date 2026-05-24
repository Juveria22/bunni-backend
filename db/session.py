"""
Async database connection via SQLAlchemy + asyncpg.
Connects to Supabase Postgres via DATABASE_URL env var.
"""

import os
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]  # postgresql+asyncpg://...
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

# Connection pool tuned for a small SaaS — adjust as you scale
engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # detect stale connections
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_db():
    """Context manager that yields a db session and always closes it."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
