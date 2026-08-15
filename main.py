"""
calendar agent, multi-tenant saas entrypoint
app wiring, schema setup, background loops, request size guard
deploy: npx @railway/cli up
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from db.session import engine, get_db
from db.models import Base
from db.repo import prune_old_data
from routers.sms import router as sms_router, drain_inflight
from routers.oauth import router as oauth_router
from services.monitoring import heartbeat_loop, init_monitoring, report
from services.redis_client import try_acquire_lock
from services.reminders import reminder_loop

PRUNE_INTERVAL_SECONDS = 24 * 60 * 60

# housekeeping only needs one replica, same as the reminder sweep
# the deletes are idempotent so this is tidiness, not correctness. ttl just
# under the interval so one prune lands per cycle rather than one per replica
PRUNE_LOCK_KEY = "housekeeping:prune"
PRUNE_LOCK_TTL_SECONDS = PRUNE_INTERVAL_SECONDS - 3600

# fixed key, every process takes it before touching schema
# concurrent CREATE TABLE IF NOT EXISTS collides on pg_type, kills a replica at boot
SCHEMA_LOCK_KEY = 8_314_027_155

# create_all makes missing tables, never adds columns to existing ones
# so anything added after first deploy goes here, idempotent, runs under the lock
COLUMN_MIGRATIONS = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS channel VARCHAR(16)",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def _prune_loop():
    """daily housekeeping, deletes are idempotent so multiple workers are fine"""
    while True:
        try:
            if await try_acquire_lock(PRUNE_LOCK_KEY, PRUNE_LOCK_TTL_SECONDS):
                async with get_db() as db:
                    messages, pending = await prune_old_data(db)
                if messages or pending:
                    logger.info(f"Pruned {messages} old messages, {pending} stale confirmations")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # never let housekeeping take the app down
            logger.exception("Prune failed, will retry next cycle")
            report("prune.failed", e)

        await asyncio.sleep(PRUNE_INTERVAL_SECONDS)


async def _prepare_schema():
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # transaction scoped, releases itself on commit
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY}
            )

        await conn.run_sync(Base.metadata.create_all)

        if conn.dialect.name == "postgresql":
            for statement in COLUMN_MIGRATIONS:
                await conn.execute(text(statement))

    logger.info("Database tables ready")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_monitoring()
    await _prepare_schema()

    background = [
        asyncio.create_task(_prune_loop()),
        asyncio.create_task(reminder_loop()),
        asyncio.create_task(heartbeat_loop()),
    ]

    yield

    # twilio never retries replies, the webhook already returned 200
    # so let them land before teardown
    await drain_inflight()

    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    await engine.dispose()


# /docs, /redoc and /openapi.json are off unless asked for. this is a webhook
# backend with no human callers, and the repo is public: publishing a clickable,
# executable map of the endpoints and their parameters only helps someone
# probing them. set EXPOSE_API_DOCS=true locally when you want them
EXPOSE_API_DOCS = os.environ.get("EXPOSE_API_DOCS", "false").lower() == "true"

app = FastAPI(
    title="Calendar Agent",
    lifespan=lifespan,
    docs_url="/docs" if EXPOSE_API_DOCS else None,
    redoc_url="/redoc" if EXPOSE_API_DOCS else None,
    openapi_url="/openapi.json" if EXPOSE_API_DOCS else None,
)


# form fields are parsed before the twilio signature check runs, so anyone who
# finds the url could make us buffer anything. real webhooks are under a kilobyte
MAX_REQUEST_BYTES = 64 * 1024


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    declared = request.headers.get("content-length")

    if declared is None:
        # a body with no declared length is chunked, and the cap below can only
        # read a header, so this was the way past the whole check. twilio always
        # sends one, so refuse the request rather than buffer it blind
        if request.method in ("POST", "PUT", "PATCH"):
            logger.warning("Rejected body with no content-length")
            return PlainTextResponse("length required", status_code=411)
    else:
        try:
            if int(declared) > MAX_REQUEST_BYTES:
                logger.warning(f"Rejected oversized request: {declared} bytes")
                return PlainTextResponse("payload too large", status_code=413)
        except ValueError:
            return PlainTextResponse("bad content-length", status_code=400)

    return await call_next(request)


app.include_router(sms_router)
app.include_router(oauth_router)


# the db probe is cached because /health is public and unauthenticated, and the
# repo is public too. uncached, anyone who knows the host could spend a pooled
# connection and a query per request, which is a cheap way to starve the pool
HEALTH_CACHE_SECONDS = 10
_health_checked_at = 0.0
_health_ok = False


@app.get("/health")
async def health():
    """health check for railway/render uptime monitoring, db probe cached"""
    global _health_checked_at, _health_ok

    now = monotonic()
    if now - _health_checked_at >= HEALTH_CACHE_SECONDS:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            _health_ok = True
        except Exception as e:
            logger.exception("Health check could not reach the database")
            report("health.db_unreachable", e)
            _health_ok = False
        _health_checked_at = now

    if not _health_ok:
        return PlainTextResponse("database unreachable", status_code=503)
    return {"status": "ok"}
