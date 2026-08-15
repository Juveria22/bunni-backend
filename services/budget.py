"""
global monthly spend ceiling

one cap across every user combined, not a cap each. covers the two things that
actually cost money, anthropic tokens and twilio segments, so a runaway loop, an
abusive sender or a drifting prompt cannot run up an unbounded bill unwatched

the rate limit bounds one user, this bounds the whole service
"""

import logging
import os
from datetime import datetime, timezone
from time import monotonic

from services.redis_client import get_redis

logger = logging.getLogger(__name__)

# everything, all users, per calendar month
MONTHLY_BUDGET_USD = float(os.environ.get("MONTHLY_BUDGET_USD", "15"))

# tracked in microdollars so redis increments stay integer, floats would drift
MICROS_PER_USD = 1_000_000
MONTHLY_BUDGET_MICROS = int(MONTHLY_BUDGET_USD * MICROS_PER_USD)

# usd per million tokens, (input, output), matched on prefix so dated model ids
# like claude-haiku-4-5-20251001 still resolve
_MODEL_PRICING = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# an unrecognised model bills at the dearer tier, so a model swap can only ever
# make us stop early, never overspend quietly
_FALLBACK_PRICING = (3.00, 15.00)

# a cache read is a tenth of input, a cache write a quarter more than input
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25

# one outbound gsm-7 segment
SMS_SEGMENT_USD = 0.0079

# keep the counter well past the month it belongs to, then let it expire itself
_RETENTION_SECONDS = 45 * 24 * 60 * 60

# the check runs per inbound message and once per sweep, so hold the answer for
# a moment rather than asking redis every time
_CHECK_CACHE_SECONDS = 5.0
_cached_spend = 0
_cached_at = 0.0

# per process fallback, a ceiling that still exists when redis does not
_local_spend: dict[str, int] = {}

# so the "budget spent" line lands once a month, not once a message
_announced: set[str] = set()


def _period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _key() -> str:
    return f"spend:{_period()}"


def price_model_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """microdollars for one anthropic call"""
    price_in, price_out = _FALLBACK_PRICING
    for prefix, pricing in _MODEL_PRICING.items():
        if model.startswith(prefix):
            price_in, price_out = pricing
            break

    billable_input = (
        input_tokens
        + cache_read_tokens * _CACHE_READ_MULTIPLIER
        + cache_write_tokens * _CACHE_WRITE_MULTIPLIER
    )
    usd = (billable_input * price_in + output_tokens * price_out) / 1_000_000
    return round(usd * MICROS_PER_USD)


def price_sms(segments: int) -> int:
    """microdollars for an outbound message of this many segments"""
    return round(segments * SMS_SEGMENT_USD * MICROS_PER_USD)


async def _add(micros: int) -> None:
    if micros <= 0:
        return

    key = _key()
    try:
        r = await get_redis()
        # SET NX gives the key its ttl in the same trip that creates it, same
        # reason as the rate limit: a crash between set and expire would leave
        # a counter that never rolls off
        await r.set(key, 0, ex=_RETENTION_SECONDS, nx=True)
        await r.incrby(key, micros)
    except Exception as e:
        logger.error(f"Could not record spend (Redis?), counting in-process: {e}")
        _local_spend[key] = _local_spend.get(key, 0) + micros


async def record_model_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    """Bill one anthropic call against the month."""
    await _add(
        price_model_call(
            model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
    )


async def record_sms(segments: int) -> None:
    """Bill one outbound message against the month."""
    await _add(price_sms(segments))


async def spent_micros() -> int:
    """what the month has cost so far, cached for a few seconds"""
    global _cached_spend, _cached_at

    now = monotonic()
    if now - _cached_at < _CHECK_CACHE_SECONDS:
        return _cached_spend

    key = _key()
    try:
        r = await get_redis()
        _cached_spend = int(await r.get(key) or 0)
    except Exception as e:
        logger.error(f"Could not read spend (Redis?), using in-process count: {e}")
        _cached_spend = _local_spend.get(key, 0)

    _cached_at = now
    return _cached_spend


async def over_budget() -> bool:
    """
    True once the month's ceiling is gone, stop doing anything that costs

    STOP still has to work when this is true, opting out is not paid work and
    a user must always be able to leave
    """
    spent = await spent_micros()
    if spent < MONTHLY_BUDGET_MICROS:
        return False

    period = _period()
    if period not in _announced:
        _announced.add(period)
        logger.critical(
            f"Monthly budget gone: ${spent / MICROS_PER_USD:.2f} of "
            f"${MONTHLY_BUDGET_USD:.2f} for {period}. Paid work is paused until "
            "the month rolls over or MONTHLY_BUDGET_USD is raised"
        )
    return True
