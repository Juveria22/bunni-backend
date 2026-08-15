"""
one shared redis connection per process

rate limiting and the sweep lock both need redis, a client per module doubles
the connection count for nothing
"""

import asyncio
import logging
import os

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None
_build_lock = asyncio.Lock()


async def get_redis() -> aioredis.Redis:
    """the shared client, built once, safe to call concurrently"""
    global _redis
    if _redis is None:
        async with _build_lock:
            # recheck, another coroutine may have built it while we waited
            if _redis is None:
                _redis = await aioredis.from_url(
                    os.environ["REDIS_URL"],
                    encoding="utf-8",
                    decode_responses=True,
                )
    return _redis


async def try_acquire_lock(key: str, ttl_seconds: int) -> bool:
    """
    best effort distributed lock, True means we hold it for ttl_seconds

    unreachable redis returns True on purpose, losing the lock service should
    degrade to every replica sweeping, which the claim table keeps correct,
    rather than stopping reminders altogether
    """
    try:
        r = await get_redis()
        return bool(await r.set(key, "1", nx=True, ex=ttl_seconds))
    except Exception as e:
        logger.warning(f"Lock {key!r} unavailable, proceeding without it: {e}")
        return True
