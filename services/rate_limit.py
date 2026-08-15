"""
per user rate limiting, fixed window counter in redis, keyed by phone
caps abuse and runaway api spend, falls back in process if redis is down
"""

import logging
from time import monotonic

from services.sanitize import mask_phone
from services.redis_client import get_redis

logger = logging.getLogger(__name__)

RATE_LIMIT = 30          # max messages
WINDOW_SECONDS = 3600    # per hour

# HELP is answered ahead of the rate limit so the keyword always works, which
# left it uncapped: a client looping on "help" bought a billable segment every
# time, forever. one reply an hour per number still satisfies the carriers
HELP_COOLDOWN_SECONDS = 3600


async def check_rate_limit(phone: str) -> tuple[bool, int]:
    """
    (allowed, count_this_window)

    the count comes back so the caller can answer the first blocked message and
    go silent after, every reply is a billable segment and a client stuck in a
    loop spends real money being told to stop
    """
    try:
        r = await get_redis()
        key = f"rl:{phone}"

        # SET NX sets the expiry in the same round trip that creates the key
        # a separate EXPIRE meant a crash in between left a key with no ttl, and
        # that user was blocked permanently once past the limit
        await r.set(key, 0, ex=WINDOW_SECONDS, nx=True)
        count = await r.incr(key)

        if count > RATE_LIMIT:
            # self heal a key left with no ttl by the old two step version
            # without this those users stay blocked forever
            if await r.ttl(key) < 0:
                logger.warning(f"Repairing missing ttl on rate limit key for {mask_phone(phone)}")
                await r.expire(key, WINDOW_SECONDS)
            logger.warning(f"Rate limited: {mask_phone(phone)} ({count} requests)")
            return False, count
        return True, count
    except Exception as e:
        # redis down must not take the app offline, but failing fully open runs
        # the agent with no ceiling at all, a misconfigured REDIS_URL would
        # quietly uncap the anthropic bill
        logger.error(f"Rate limit check failed (Redis?), falling back in-process: {e}")
        return _local_check(phone)


async def claim_help_reply(phone: str) -> bool:
    """
    True if we should answer HELP for this number now, False to stay quiet

    SET NX is the whole cooldown, first caller in the window wins the reply
    """
    try:
        r = await get_redis()
        return bool(
            await r.set(f"help:{phone}", "1", nx=True, ex=HELP_COOLDOWN_SECONDS)
        )
    except Exception as e:
        logger.error(f"Help cooldown check failed (Redis?), falling back in-process: {e}")
        return _local_help_claim(phone)


# per process fallback, less accurate than redis across workers, the point is a
# ceiling that still exists when redis does not
_local_counts: dict[str, tuple[float, int]] = {}
_local_help: dict[str, float] = {}


def _local_help_claim(phone: str) -> bool:
    now = monotonic()
    last = _local_help.get(phone)
    if last is not None and now - last < HELP_COOLDOWN_SECONDS:
        return False

    if len(_local_help) > 10_000:  # bounded, it is a fallback
        _local_help.clear()

    _local_help[phone] = now
    return True


def _local_check(phone: str) -> tuple[bool, int]:
    now = monotonic()
    window_start, count = _local_counts.get(phone, (now, 0))

    if now - window_start >= WINDOW_SECONDS:
        window_start, count = now, 0

    count += 1
    _local_counts[phone] = (window_start, count)

    if len(_local_counts) > 10_000:  # bounded, it is a fallback
        _local_counts.clear()

    if count > RATE_LIMIT:
        logger.warning(f"Rate limited in-process: {mask_phone(phone)} ({count} requests)")
        return False, count
    return True, count
