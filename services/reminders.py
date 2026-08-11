"""
SMS reminders. Sweeps everyone's calendar on a timer and texts them an hour
before an event and again as it starts.

Google's own reminders fire as popups and emails, which is exactly the thing
people miss. This is the product: a text, in the same voice as the rest of the
agent, that reads like a friend nudging you rather than an alert.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

import anthropic

from db.session import get_db
from db.repo import list_onboarded_users, claim_reminder
from services.calendar import list_events, now_local
from services.google_oauth import build_calendar_service, forget_calendar_service
from services.sms import send_message

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

# How often the sweep runs. Google's api is free, so the tick is really about
# how tightly the windows below can be drawn.
SWEEP_INTERVAL_SECONDS = 120

# Minutes-until-start ranges that trigger each reminder. Wider than the tick so
# a slow sweep can't step over an event; the claim in the database is what
# stops the overlap turning into two texts.
SOON_WINDOW = (50, 70)
NOW_WINDOW = (-2, 6)

# Haiku, not Sonnet. This is one short line with no tools and no history —
# the cheapest model that can still judge that a flight matters more than a
# coffee. Roughly $0.0004 a reminder, against $0.0079 to actually send it.
REMINDER_MODEL = "claude-haiku-4-5-20251001"
REMINDER_MAX_TOKENS = 60

_REMINDER_PROMPT = """you write one text reminding someone about something on their calendar. you are their assistant but you text like a friend.

what matters:
- judge from the title how much this one counts. a flight, an interview, an exam, a wedding, surgery, a deadline deserve real urgency and the specifics that help (time, place). a coffee, a standup, a gym session should be light and short
- "soon" means it starts in about an hour. "now" means it is starting right about now
- never invent anything you weren't given. no made up locations, no made up people

tone rules (non-negotiable):
- all lowercase always
- no emojis
- no periods at the end of sentences ever
- no em dashes or en dashes ever, use a comma or just stop
- fragments are fine, preferred even
- ONE line, under 140 characters, this is a single text
- do not start every message the same way, vary it

good examples:
  "heads up, flight to chicago in an hour, terminal b"
  "interview w/ nova in an hour, u got this"
  "dentist at 3, leave in like 10"
  "standup starting"
  "gym in an hour if ur still going"
  "ur exam starts now, good luck"
"""


def _fallback(title: str, kind: str) -> str:
    """Used when the model is unreachable. A plain reminder still beats silence."""
    title = (title or "something").lower()
    return f"heads up, {title} in an hour" if kind == "soon" else f"{title} starting now"


async def write_reminder(event: dict, kind: str, minutes: float) -> str:
    """One line of text for this reminder. Never raises."""
    details = {
        "title": event.get("summary") or "untitled",
        "when": "in about an hour" if kind == "soon" else "starting now",
        "minutes_until_start": round(minutes),
    }
    if event.get("location"):
        details["location"] = event["location"]

    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=REMINDER_MODEL,
                max_tokens=REMINDER_MAX_TOKENS,
                system=_REMINDER_PROMPT,
                messages=[{"role": "user", "content": json.dumps(details)}],
            ),
            timeout=8.0,
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        # A model that rambles would cost an extra sms segment
        if text and len(text) <= 160:
            return text
        logger.warning(f"Reminder text unusable ({len(text)} chars), using fallback")
    except Exception as e:
        logger.warning(f"Reminder model failed: {e}")

    return _fallback(event.get("summary"), kind)


def _due_kind(minutes: float) -> str | None:
    if SOON_WINDOW[0] <= minutes <= SOON_WINDOW[1]:
        return "soon"
    if NOW_WINDOW[0] <= minutes <= NOW_WINDOW[1]:
        return "now"
    return None


def _worth_reminding(event: dict) -> bool:
    """
    Same shape of filtering as the clash check. Something the user marked free,
    or an invite they declined, isn't worth a text, and every skipped event is
    a message not paid for.
    """
    if event.get("status") == "cancelled":
        return False
    # All-day events have no start time, so "an hour before" is meaningless
    if "dateTime" not in event.get("start", {}):
        return False
    if event.get("transparency") == "transparent":
        return False
    return not any(
        a.get("self") and a.get("responseStatus") == "declined"
        for a in event.get("attendees", [])
    )


async def sweep_user(phone: str, refresh_token: str) -> int:
    """Text this user about anything due. Returns how many were sent."""
    service = await build_calendar_service(refresh_token)
    now = now_local()

    events = await list_events(
        service,
        time_min=now + timedelta(minutes=NOW_WINDOW[0]),
        time_max=now + timedelta(minutes=SOON_WINDOW[1]),
        max_results=25,
    )

    sent = 0
    for event in events:
        if not _worth_reminding(event):
            continue

        start = datetime.fromisoformat(event["start"]["dateTime"])
        kind = _due_kind((start - now).total_seconds() / 60)
        if not kind:
            continue

        # Claim before sending. If another worker got there first this is a
        # no-op, which is the whole point.
        async with get_db() as db:
            if not await claim_reminder(db, phone, event["id"], kind):
                continue

        text = await write_reminder(event, kind, (start - now).total_seconds() / 60)
        await send_message(phone, text)
        logger.info(f"Reminded {phone} ({kind}): {event.get('summary')!r}")
        sent += 1

    return sent


async def run_sweep() -> int:
    """One pass over everyone. A single user's failure doesn't stop the rest."""
    async with get_db() as db:
        users = await list_onboarded_users(db)

    sent = 0
    for phone, refresh_token in users:
        try:
            sent += await sweep_user(phone, refresh_token)
        except Exception as e:
            if "invalid_grant" in str(e).lower():
                # They revoked us. Nothing to do until they reconnect.
                forget_calendar_service(refresh_token)
                logger.warning(f"Skipping reminders for {phone}, google access revoked")
            else:
                logger.exception(f"Reminder sweep failed for {phone}: {e}")

    return sent


async def reminder_loop():
    while True:
        try:
            sent = await run_sweep()
            if sent:
                logger.info(f"Reminder sweep sent {sent} texts")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder sweep failed, will retry next tick")

        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
