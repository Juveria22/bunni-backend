"""
Google Calendar operations. Every function takes a `service` object
that's already scoped to a specific user's credentials.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)
CALENDAR_ID = "primary"

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


async def find_conflicting_events(
    service,
    start_dt: datetime,
    end_dt: datetime,
    max_results: int = 10,
) -> list[dict]:
    """
    Existing events that overlap [start_dt, end_dt).

    Google's timeMin/timeMax filter already means "overlaps this range", so the
    remaining work is dropping things that aren't real clashes: all-day events,
    slots the user marked free, and invites they declined.
    """
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute()

    conflicts = []
    for event in result.get("items", []):
        if event.get("status") == "cancelled":
            continue
        # All-day events carry "date" instead of "dateTime" — they blanket the
        # whole day and would flag every single event as a conflict
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
    start_time: str,
    duration_minutes: int = 60,
    location: Optional[str] = None,
    reminder_minutes: int = 60,
    description: Optional[str] = None,
) -> dict:
    start_dt, end_dt = parse_event_window(date, start_time, duration_minutes)

    body = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE_NAME},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE_NAME},
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

    event = service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    logger.info(f"Created event: {event['id']} — {title}")
    return event


async def search_future_events(service, query: str, scope: str = "future", max_results: int = 50) -> list[dict]:
    params = {
        "calendarId": CALENDAR_ID,
        "q": query,
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if scope == "future":
        params["timeMin"] = datetime.now(timezone.utc).isoformat()

    result = service.events().list(**params).execute()
    events = result.get("items", [])

    q = query.lower()
    return [
        e for e in events
        if q in e.get("summary", "").lower() or q in e.get("description", "").lower()
    ]


async def update_event_reminders(service, events: list[dict], reminder_minutes_list: list[int]) -> int:
    overrides = [
        {"method": m, "minutes": mins}
        for mins in reminder_minutes_list
        for m in ("popup", "email")
    ]
    body = {"reminders": {"useDefault": False, "overrides": overrides}}
    updated = 0

    for event in events:
        try:
            service.events().patch(
                calendarId=CALENDAR_ID,
                eventId=event["id"],
                body=body,
            ).execute()
            logger.info(f"Updated: {event.get('summary')} ({event['id']})")
            updated += 1
        except HttpError as e:
            logger.error(f"Failed to patch {event['id']}: {e}")

    return updated
