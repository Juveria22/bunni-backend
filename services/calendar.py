"""
google calendar operations
every function takes a service already scoped to one user's credentials
also owns the timezone and the date/time math the agent reasons over
"""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from services.sanitize import scrub

logger = logging.getLogger(__name__)
CALENDAR_ID = "primary"

# google's own all day default, 9am the day before
# all day reminders count back from midnight, so the normal 60 minute default
# would fire at 11pm the previous night
ALL_DAY_REMINDER_MINUTES = 900

# google rejects more than five reminder overrides per event
# we emit popup + email per offset, so three offsets made six and the patch
# failed with a generic error that retrying could never fix
MAX_REMINDER_OVERRIDES = 5


async def _execute(request):
    """
    google's python client is sync, .execute() from a coroutine blocks the whole
    event loop for the http round trip and stalls every other webhook
    """
    return await asyncio.to_thread(request.execute)

# named zone, not a fixed offset
# a hardcoded -04:00 is edt and books everything an hour early once est starts
TIMEZONE_NAME = "America/New_York"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)


def now_local() -> datetime:
    """now in the user's zone, use for anything date shaped"""
    return datetime.now(TIMEZONE)


def parse_event_window(
    date: str,
    start_time: str,
    duration_minutes: int = 60,
) -> tuple[datetime, datetime]:
    """
    (start, end) for a timed event
    shared by create and the clash check so both reason about the same slot

    offset comes off the date, -04:00 in summer, -05:00 in winter
    duration is added in utc so an event over a dst change keeps its length
    """
    start_dt = datetime.fromisoformat(f"{date}T{start_time}:00").replace(tzinfo=TIMEZONE)
    end_dt = (
        start_dt.astimezone(timezone.utc) + timedelta(minutes=duration_minutes)
    ).astimezone(TIMEZONE)
    return start_dt, end_dt


def all_day_window(date: str, end_date: str | None = None) -> tuple[datetime, datetime]:
    """
    real span an all day event blocks, local midnight to local midnight after
    the last day. used for clash checks, all day means the whole day not 00:00
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
    the start/end half of an event body, shared by create and reschedule

    all day events use plain dates with no time, that is what puts them in the
    top strip instead of at midnight. google's end is exclusive, so a one day
    event ends on the following day
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
    (date, start_time, duration_minutes, all_day) for an existing event
    lets a reschedule keep whatever the user did not ask to change
    """
    start, end = event.get("start", {}), event.get("end", {})

    if "dateTime" in start:
        s = datetime.fromisoformat(start["dateTime"])
        e = datetime.fromisoformat(end["dateTime"])
        minutes = int((e - s).total_seconds() // 60)
        return s.date().isoformat(), f"{s.hour:02d}:{s.minute:02d}", minutes, False

    return datetime.fromisoformat(start["date"]).date().isoformat(), None, 0, True


def all_day_span_days(event: dict) -> int:
    """
    how many days an all day event covers, google's end is exclusive so one day
    spans 1. anything not all day spans 1 too

    needed on reschedule, without it moving a five day trip rebuilds it as one
    """
    start, end = event.get("start", {}), event.get("end", {})
    if "date" not in start or "date" not in end:
        return 1
    first = datetime.fromisoformat(start["date"]).date()
    last = datetime.fromisoformat(end["date"]).date()
    return max((last - first).days, 1)


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
    move an existing event

    patch replaces the whole start/end objects, so switching a timed event to
    all day drops its dateTime instead of leaving a stale one
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
    existing events overlapping [start_dt, end_dt)

    google's timeMin/timeMax already means "overlaps this range", the rest is
    dropping non clashes: free slots and declined invites
    exclude_event_ids stops a reschedule clashing with the event it is moving
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
        # all day events count, a trip or a day off is a real reason not to book
        # the transparency check is what keeps birthdays and holidays out, those
        # are marked free and imported ones live on their own calendars
        if event.get("transparency") == "transparent":
            continue
        # a declined invite is not holding the slot
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
    logger.info(f"Created event: {event['id']} - {title}")
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
    everything in a window

    no keyword filtering of its own, the model reads the list and decides
    substring matching on a title nobody types exactly is what made
    "office day saturday" fail to find "Office Day"
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
    one event flattened for the model, weekday spelled out because people say
    "saturday's office event", not a date

    title and location get scrubbed, anyone can put an event on a primary
    calendar by sending an invite, so those two are where third party text
    enters the agent's context
    """
    date_str, start_time, duration, all_day = read_event_window(event)
    day = datetime.fromisoformat(date_str).strftime("%A %b %d").lower()

    summary = {
        "id": event["id"],
        "title": scrub(event.get("summary")) or "untitled",
        "day": day,
        "date": date_str,
        "all_day": all_day,
    }
    if not all_day:
        summary["start"] = start_time
        summary["duration_minutes"] = duration
    if event.get("location"):
        summary["location"] = scrub(event["location"])
    if event.get("recurringEventId"):
        # only the next few occurrences are shown, so say it repeats instead of
        # letting the model conclude those are all of them
        summary["repeats"] = True
    return summary


def build_reminder_overrides(reminder_minutes_list: list[int]) -> tuple[list[dict], list[int]]:
    """
    (overrides, offsets actually applied), never over google's cap of five

    two offsets fit with popup and email, past that the email copy goes so up to
    five distinct times still land, past five the extras get cut
    caller gets the applied list so it can say what really happened
    """
    offsets = sorted({m for m in reminder_minutes_list if m >= 0}, reverse=True)

    methods = ("popup", "email") if len(offsets) * 2 <= MAX_REMINDER_OVERRIDES else ("popup",)
    applied = offsets[: MAX_REMINDER_OVERRIDES // len(methods)]

    overrides = [
        {"method": method, "minutes": minutes}
        for minutes in applied
        for method in methods
    ]
    return overrides, applied


async def update_event_reminders(
    service,
    event_ids: list[str],
    reminder_minutes_list: list[int],
) -> tuple[int, list[int]]:
    """patch reminders onto each event, returns (updated count, offsets applied)"""
    overrides, applied = build_reminder_overrides(reminder_minutes_list)
    if not overrides:
        return 0, []

    if len(applied) < len({m for m in reminder_minutes_list if m >= 0}):
        logger.info(f"Trimmed reminder offsets to {applied} to stay under Google's cap")

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

    return updated, applied
