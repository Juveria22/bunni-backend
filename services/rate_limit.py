"""
Rate limiting via Redis.
30 requests per user per hour — prevents abuse and runaway API costs.
Uses a simple sliding window counter.
"""

import os
import logging
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

RATE_LIMIT = 30          # max messages
WINDOW_SECONDS = 3600    # per hour

_redis = None


async def _get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            os.environ["REDIS_URL"],
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis


async def check_rate_limit(phone: str) -> bool:
    """
    Returns True if the request should proceed, False if rate limited.
    Increments the counter in Redis with a sliding TTL.
    """
    try:
        r = await _get_redis()
        key = f"rl:{phone}"
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, WINDOW_SECONDS)
        if count > RATE_LIMIT:
            logger.warning(f"Rate limited: {phone} ({count} requests)")
            return False
        return True
    except Exception as e:
        # If Redis is down, fail open — don't block the user
        logger.error(f"Rate limit check failed (Redis?): {e}")
        return True
