"""
Calendar Agent — multi-tenant SaaS entrypoint.
to deploy: npx @railway/cli up
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from db.session import engine
from db.models import Base
from routers.sms import router as sms_router
from routers.oauth import router as oauth_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup (idempotent — safe to run every deploy)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ready")
    yield
    await engine.dispose()


app = FastAPI(title="Calendar Agent", lifespan=lifespan)

app.include_router(sms_router)
app.include_router(oauth_router)


@app.get("/health")
async def health():
    """Health check for Railway/Render uptime monitoring."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}
