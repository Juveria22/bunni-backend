"""
Google Calendar operations. Every function takes a `service` object
that's already scoped to a specific user's credentials.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)
CALENDAR_ID = "primary"


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
    start_dt = datetime.fromisoformat(f"{date}T{start_time}:00-04:00")
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    body = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/New_York"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/New_York"},
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
