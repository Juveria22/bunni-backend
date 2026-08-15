"""
error monitoring and failure counters

the failure this exists for is the quiet one. a sweep that skips every user, a
send that always fails, a cache that never engages, a token that can no longer
be decrypted: none of those raise anything a person sees, they just show up on
the bill or in a complaint weeks later

two halves, and the counters are the half that always works:
  counters   in process, always on, summarised on a heartbeat so a run of
             failures cannot pass unnoticed even with no external service
  sentry     optional, set SENTRY_DSN. gives stack traces and alerting

nothing here may raise. monitoring that can take the request down is worse than
no monitoring
"""

import asyncio
import logging
import os
from collections import Counter
from time import monotonic

logger = logging.getLogger(__name__)

# how often the heartbeat summarises. long enough to stay out of the way, short
# enough that a bad deploy is visible well inside an hour
HEARTBEAT_INTERVAL_SECONDS = 15 * 60

# say something even when nothing failed, so silence always means "the loop is
# dead", never "everything is fine"
_counts: Counter = Counter()
_totals: Counter = Counter()
_started_at = monotonic()

# events worth waking up for, counted like the rest but also sent to sentry and
# logged at error level. everything else is a tally
ALERT_EVENTS = frozenset({
    "agent.error",
    "reply.delivery_failed",
    "reminder.send_failed",
    "sweep.failed",
    "sweep.overrun",
    "budget.exhausted",
    "token.undecryptable",
    "agent.cache_miss",
    "redis.unavailable",
})

_sentry = None


def init_monitoring() -> None:
    """
    Wire up sentry if it is configured. Safe to call when it is not.

    Absent a DSN this is a no-op and the counters carry the whole load, which
    is the expected setup for a small deployment.
    """
    global _sentry

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("SENTRY_DSN not set, error monitoring is counters and logs only")
        return

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            # a calendar agent's traces carry event titles and phone numbers,
            # so send no request bodies or user identifiers by default
            send_default_pii=False,
            traces_sample_rate=0.0,
            environment=os.environ.get("RAILWAY_ENVIRONMENT", "production"),
        )
        _sentry = sentry_sdk
        logger.info("Sentry error monitoring enabled")
    except Exception as e:
        # a broken monitoring config must never stop the app booting
        logger.error(f"Could not start Sentry, continuing without it: {e}")


def count(event: str, n: int = 1) -> None:
    """Tally something worth knowing the rate of."""
    _counts[event] += n
    _totals[event] += n


def report(event: str, exc: BaseException | None = None, **context) -> None:
    """
    Record a failure: counted, logged, and sent to sentry when configured.

    Takes no phone numbers or message bodies. Pass already-masked values.
    """
    count(event)

    detail = " ".join(f"{k}={v}" for k, v in context.items())
    if event in ALERT_EVENTS:
        logger.error(f"[{event}] {detail}".rstrip())
    else:
        logger.warning(f"[{event}] {detail}".rstrip())

    if _sentry is None:
        return

    try:
        with _sentry.push_scope() as scope:
            scope.set_tag("event", event)
            for key, value in context.items():
                scope.set_extra(key, value)
            if exc is not None:
                _sentry.capture_exception(exc)
            else:
                _sentry.capture_message(event)
    except Exception as e:
        logger.error(f"Could not report to Sentry: {e}")


def snapshot() -> dict[str, int]:
    """Totals since boot, for tests and the heartbeat."""
    return dict(_totals)


def _drain() -> dict[str, int]:
    """Counts since the last heartbeat, then reset."""
    window = dict(_counts)
    _counts.clear()
    return window


async def heartbeat_loop() -> None:
    """
    Summarise the window on a timer, forever

    It logs on every tick, including quiet ones. A heartbeat that only spoke up
    on failure would be indistinguishable from a dead loop.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

        try:
            window = _drain()
            uptime_min = (monotonic() - _started_at) / 60

            if not window:
                logger.info(f"heartbeat | quiet | up {uptime_min:.0f}m")
                continue

            alerts = {k: v for k, v in window.items() if k in ALERT_EVENTS}
            body = " ".join(f"{k}={v}" for k, v in sorted(window.items()))

            if alerts:
                logger.error(
                    f"heartbeat | up {uptime_min:.0f}m | {body} | "
                    f"failures this window: {sorted(alerts)}"
                )
            else:
                logger.info(f"heartbeat | up {uptime_min:.0f}m | {body}")
        except asyncio.CancelledError:
            raise
        except Exception:
            # the heartbeat is the thing that notices problems, it does not get
            # to become one
            logger.exception("Heartbeat failed, will summarise again next tick")
