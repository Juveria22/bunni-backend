"""
Google Calendar operations. Every function takes a `service` object
that's already scoped to a specific user's credentials.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)
CALENDAR_ID = "primary"


async def _execute(request):
    """
    Google's python client is synchronous. Calling .execute() directly from a
    coroutine blocks the whole event loop for the length of the http round
    trip, which stalls every other user's webhook. Hand it to a thread.
    """
    return await asyncio.to_thread(request.execute)

# Users are in eastern time. This must be a named zone, not a fixed offset —
# a hardcoded -04:00 is EDT and silently books everything an hour early once
# EST starts in november.
TIMEZONE_NAME = "America/New_York"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)


def now_local() -> datetime:
    """Current time in the user's zone. Use this for anything date-shaped."""
    return datetime.now(TIMEZONE)


def parse_event_window(
    date: str,
    start_time: str,
    duration_minutes: int = 60,
) -> tuple[datetime, datetime]:
    """
    (start, end) for an event. Shared by create_event and the conflict check
    so both reason about exactly the same slot.

    The offset is derived from the date, so it's -04:00 in summer and -05:00
    in winter. Duration is added in UTC so an event spanning a dst change is
    still the length the user asked for.
    """
    start_dt = datetime.fromisoformat(f"{date}T{start_time}:00").replace(tzinfo=TIMEZONE)
    end_dt = (
        start_dt.astimezone(timezone.utc) + timedelta(minutes=duration_minutes)
    ).astimezone(TIMEZONE)
    return start_dt, end_dt


def all_day_window(date: str, end_date: str | None = None) -> tuple[datetime, datetime]:
    """
    The real span an all-day event blocks: local midnight on the first day
    through local midnight after the last. Used to find what it collides with,
    since "all day" means the whole day, not a slot at 00:00.
    """
    start_day = datetime.fromisoformat(date).date()
    last_day = datetime.fromisoformat(end_date).date() if end_date else start_day
    return (
        datetime.combine(start_day, time.min, tzinfo=TIMEZONE),
        datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=TIMEZONE),
    )


def build_time_body(
    date: str,
    start_time: str | None = None,
    duration_minutes: int = 60,
    all_day: bool = False,
    end_date: str | None = None,
) -> dict:
    """
    The start/end half of an event body, shared by create and reschedule.

    All-day events use plain dates with no time at all — that's what makes
    Google render them in the top strip instead of at midnight. Google treats
    `end` as exclusive, so a one-day event ends on the following day.
    """
    if all_day:
        start_day = datetime.fromisoformat(date).date()
        last_day = datetime.fromisoformat(end_date).date() if end_date else start_day
        return {
            "start": {"date": start_day.isoformat()},
            "end": {"date": (last_day + timedelta(days=1)).isoformat()},
        }

    start_dt, end_dt = parse_event_window(date, start_time, duration_minutes)
    return {
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE_NAME},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE_NAME},
    }


def read_event_window(event: dict) -> tuple[str, str | None, int, bool]:
    """
    (date, start_time, duration_minutes, all_day) for an existing event, so a
    reschedule can keep whatever the user didn't ask to change.
    """
    start, end = event.get("start", {}), event.get("end", {})

    if "dateTime" in start:
        s = datetime.fromisoformat(start["dateTime"])
        e = datetime.fromisoformat(end["dateTime"])
        minutes = int((e - s).total_seconds() // 60)
        return s.date().isoformat(), f"{s.hour:02d}:{s.minute:02d}", minutes, False

    return datetime.fromisoformat(start["date"]).date().isoformat(), None, 0, True


async def update_event_time(
    service,
    event_id: str,
    date: str,
    start_time: str | None = None,
    duration_minutes: int = 60,
    all_day: bool = False,
    end_date: str | None = None,
) -> dict:
    """
    Move an existing event. Patch replaces the whole start/end objects, so
    switching a timed event to all-day drops its dateTime rather than leaving
    a stale one behind.
    """
    event = await _execute(service.events().patch(
        calendarId=CALENDAR_ID,
        eventId=event_id,
        body=build_time_body(date, start_time, duration_minutes, all_day, end_date),
    ))
    logger.info(f"Moved event {event_id} to {date} {start_time or '(all day)'}")
    return event


async def find_conflicting_events(
    service,
    start_dt: datetime,
    end_dt: datetime,
    max_results: int = 50,
    exclude_event_ids: set[str] | None = None,
) -> list[dict]:
    """
    Existing events that overlap [start_dt, end_dt).

    Google's timeMin/timeMax filter already means "overlaps this range", so the
    remaining work is dropping things that aren't real clashes: all-day events,
    slots the user marked free, and invites they declined.

    exclude_event_ids keeps a reschedule from finding the event it's moving and
    reporting it as a clash with itself.
    """
    excluded = exclude_event_ids or set()
    result = await _execute(service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ))

    conflicts = []
    for event in result.get("items", []):
        if event.get("id") in excluded:
            continue
        if event.get("status") == "cancelled":
            continue
        # All-day events carry "date" instead of "dateTime". Birthdays and
        # holidays sit on the calendar all year and don't hold a time slot,
        # so they'd false-positive on essentially everything
        if "dateTime" not in event.get("start", {}):
            continue
        # transparency=transparent is Google's "show me as free"
        if event.get("transparency") == "transparent":
            continue
        # An invite the user declined isn't holding the slot
        if any(
            a.get("self") and a.get("responseStatus") == "declined"
            for a in event.get("attendees", [])
        ):
            continue
        conflicts.append(event)

    return conflicts


async def create_event(
    service,
    title: str,
    date: str,
    start_time: Optional[str] = None,
    duration_minutes: int = 60,
    location: Optional[str] = None,
    reminder_minutes: int = 60,
    description: Optional[str] = None,
    all_day: bool = False,
    end_date: Optional[str] = None,
) -> dict:
    body = {
        "summary": title,
        **build_time_body(date, start_time, duration_minutes, all_day, end_date),
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": reminder_minutes},
                {"method": "email", "minutes": reminder_minutes},
            ],
        },
    }
    if location:    body["location"] = location
    if description: body["description"] = description

    event = await _execute(service.events().insert(calendarId=CALENDAR_ID, body=body))
    logger.info(f"Created event: {event['id']} — {title}")
    return event


async def get_event(service, event_id: str) -> dict:
    return await _execute(service.events().get(calendarId=CALENDAR_ID, eventId=event_id))


async def delete_event(service, event_id: str) -> None:
    await _execute(service.events().delete(calendarId=CALENDAR_ID, eventId=event_id))
    logger.info(f"Deleted event {event_id}")


async def list_events(
    service,
    time_min: datetime,
    time_max: datetime,
    query: Optional[str] = None,
    max_results: int = 40,
) -> list[dict]:
    """
    Everything on the calendar in a window. Deliberately does no keyword
    filtering of its own — the model reads the list and decides what matches.
    Substring matching on a title the user never types exactly is what made
    "office day saturday" fail to find "Office Day".
    """
    params = {
        "calendarId": CALENDAR_ID,
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max_results,
    }
    if query:
        params["q"] = query

    result = await _execute(service.events().list(**params))
    return [e for e in result.get("items", []) if e.get("status") != "cancelled"]


def summarize_event(event: dict) -> dict:
    """
    An event flattened for the model to reason over. The weekday is spelled out
    because users say "saturday's office event", not a date.
    """
    date_str, start_time, duration, all_day = read_event_window(event)
    day = datetime.fromisoformat(date_str).strftime("%A %b %d").lower()

    summary = {
        "id": event["id"],
        "title": event.get("summary") or "untitled",
        "day": day,
        "date": date_str,
        "all_day": all_day,
    }
    if not all_day:
        summary["start"] = start_time
        summary["duration_minutes"] = duration
    if event.get("location"):
        summary["location"] = event["location"]
    return summary


async def update_event_reminders(service, event_ids: list[str], reminder_minutes_list: list[int]) -> int:
    overrides = [
        {"method": m, "minutes": mins}
        for mins in reminder_minutes_list
        for m in ("popup", "email")
    ]
    body = {"reminders": {"useDefault": False, "overrides": overrides}}
    updated = 0

    for event_id in event_ids:
        try:
            await _execute(service.events().patch(
                calendarId=CALENDAR_ID,
                eventId=event_id,
                body=body,
            ))
            logger.info(f"Updated reminders on {event_id}")
            updated += 1
        except HttpError as e:
            logger.error(f"Failed to patch {event_id}: {e}")

    return updated
