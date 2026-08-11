"""
Calendar Agent — multi-tenant SaaS entrypoint.
to deploy: npx @railway/cli up
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from db.session import engine, get_db
from db.models import Base
from db.repo import prune_old_data
from routers.sms import router as sms_router
from routers.oauth import router as oauth_router
from services.reminders import reminder_loop

PRUNE_INTERVAL_SECONDS = 24 * 60 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def _prune_loop():
    """
    Housekeeping, once a day. Deletes are idempotent, so several workers each
    running this is harmless.
    """
    while True:
        try:
            async with get_db() as db:
                messages, pending = await prune_old_data(db)
            if messages or pending:
                logger.info(f"Pruned {messages} old messages, {pending} stale confirmations")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let housekeeping take the app down
            logger.exception("Prune failed, will retry next cycle")

        await asyncio.sleep(PRUNE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup (idempotent — safe to run every deploy)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ready")

    background = [
        asyncio.create_task(_prune_loop()),
        asyncio.create_task(reminder_loop()),
    ]

    yield

    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
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
