"""
Rate limiting via Redis.
30 requests per user per hour — prevents abuse and runaway API costs.
Uses a simple sliding window counter.
"""

import os
import logging
from time import monotonic

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
        # Redis being down must not take the app offline, but failing fully
        # open means every message runs the agent with no ceiling at all —
        # a misconfigured REDIS_URL would quietly uncap the anthropic bill.
        logger.error(f"Rate limit check failed (Redis?), falling back in-process: {e}")
        return _local_check(phone)


# Per-process fallback. Less accurate than Redis across workers, but the point
# is a ceiling that still exists when Redis doesn't.
_local_counts: dict[str, tuple[float, int]] = {}


def _local_check(phone: str) -> bool:
    now = monotonic()
    window_start, count = _local_counts.get(phone, (now, 0))

    if now - window_start >= WINDOW_SECONDS:
        window_start, count = now, 0

    count += 1
    _local_counts[phone] = (window_start, count)

    if len(_local_counts) > 10_000:  # bounded, it's a fallback
        _local_counts.clear()

    if count > RATE_LIMIT:
        logger.warning(f"Rate limited in-process: {phone} ({count} requests)")
        return False
    return True
