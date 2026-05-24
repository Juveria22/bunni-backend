"""
Claude agent. Takes a user message + their calendar service,
routes to the right tool, executes it, returns a reply in gen-z tone.
"""

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
import asyncio

import anthropic

from services.calendar import create_event, search_future_events, update_event_reminders

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

TOOLS = [
    {
        "name": "create_calendar_event",
        "description": (
            "Use when the user wants to schedule a NEW event, meeting, appointment, "
            "or any one-time calendar entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title":                  {"type": "string"},
                "date":                   {"type": "string", "description": "YYYY-MM-DD. Resolve relative dates from today."},
                "start_time":             {"type": "string", "description": "HH:MM 24hr"},
                "duration_minutes":       {"type": "integer", "default": 60},
                "location":               {"type": "string"},
                "reminder_minutes_before":{"type": "integer", "default": 60},
                "description":            {"type": "string"},
            },
            "required": ["title", "date", "start_time"],
        },
    },
    {
        "name": "update_reminders_by_query",
        "description": (
            "Use when the user wants to SET or UPDATE reminders on EXISTING events "
            "matching a keyword like 'all meetings with Zain' or 'every dentist appt'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search_query":            {"type": "string"},
                "reminder_offsets_days":   {"type": "array", "items": {"type": "integer"}},
                "reminder_offsets_hours":  {"type": "array", "items": {"type": "integer"}},
                "scope":                   {"type": "string", "enum": ["future", "all"], "default": "future"},
            },
            "required": ["search_query", "reminder_offsets_days"],
        },
    },
    {
        "name": "ask_clarification",
        "description": "Use ONLY when intent is genuinely unclear or a required field is missing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
            },
            "required": ["question"],
        },
    },
]


# ---------- prompt caching ----------
# The static portion of the system prompt never changes.
# We use Anthropic's prompt caching (cache_control) so the API
# only processes it once per cache TTL (~5 min), then reads from
# a cache on subsequent calls. Saves ~400 tokens of compute per request.
# The date is injected separately as a tiny uncached block so the
# static block stays byte-for-byte identical across all users/requests.

_STATIC_PROMPT = """you are a personal ai calendar assistant. the user texts you and you manage their google calendar.

you have three tools, always use exactly one:
1. create_calendar_event - for any new event
2. update_reminders_by_query - for adding/changing reminders on existing events
3. ask_clarification - only if you genuinely cannot proceed without more info

rules:
- if a nj city is mentioned without a state, append ", nj" to the location
- "week before and day before" → reminder_offsets_days: [7, 1]
- be confident, if intent is clear act on it

tone rules (non-negotiable):
- all lowercase always
- no emojis
- no periods at the end of sentences ever
- no em dashes or en dashes ever. replace with a comma or just end the thought
  bad: "added dentist — reminder set 1hr before"
  good: "added dentist, reminder 1hr before"
- no formal sentence structure. fragments are fine. preferred even
- gen-z casual. like texting a friend who happens to manage your calendar
- one or two lines max. this is sms not email
- examples of good replies:
  "added meeting with jake tomorrow at noon, reminder 1hr before"
  "got it, updated 3 zain events with 7 day and 1 day reminders"
  "what time tho"
  "couldn't find any events with that name\""""


def build_system_prompt() -> list[dict]:
    """
    Returns a system prompt as a list of content blocks.
    The large static block is marked for caching. Anthropic's API
    will cache it for ~5 minutes, so repeat callers skip reprocessing it.
    The tiny date block is uncached since it changes daily.
    """
    today = datetime.now(timezone.utc).strftime('%A, %B %d, %Y')
    return [
        {
            "type": "text",
            "text": _STATIC_PROMPT,
            "cache_control": {"type": "ephemeral"},  # cache this block
        },
        {
            "type": "text",
            "text": f"today: {today} (eastern time)",
            # no cache_control, tiny and date-specific, not worth caching
        },
    ]


async def run_agent(user_message: str, calendar_service) -> str:
    response = await asyncio.wait_for(client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=build_system_prompt(),
        tools=TOOLS,
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    ), timeout=10.0)

    # log cache hit/miss so you can verify it's working
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_created = getattr(usage, "cache_creation_input_tokens", 0)
    logger.info(f"tokens | in:{usage.input_tokens} out:{usage.output_tokens} cache_read:{cache_read} cache_created:{cache_created}")

    if response.stop_reason != "tool_use":
        text = next((b.text for b in response.content if b.type == "text"), None)
        return text or "idk what you mean, try again"

    tool = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool:
        return "something broke try again"

    logger.info(f"Tool: {tool.name} | Input: {json.dumps(tool.input, indent=2)}")

    if tool.name == "create_calendar_event":
        return await _handle_create(tool.input, calendar_service)
    elif tool.name == "update_reminders_by_query":
        return await _handle_update_reminders(tool.input, calendar_service)
    elif tool.name == "ask_clarification":
        return tool.input["question"].lower()

    return "something went wrong try again"


async def _handle_create(args: dict, service) -> str:
    await create_event(
        service,
        title=args["title"],
        date=args["date"],
        start_time=args["start_time"],
        duration_minutes=args.get("duration_minutes", 60),
        location=args.get("location"),
        reminder_minutes=args.get("reminder_minutes_before", 60),
        description=args.get("description"),
    )

    time_str = _fmt_time(args["start_time"])
    loc = f" at {args['location']}" if args.get("location") else ""
    reminder = _fmt_reminder(args.get("reminder_minutes_before", 60))

    return f"added {args['title']} on {args['date']} at {time_str}{loc}, reminder {reminder} before"


async def _handle_update_reminders(args: dict, service) -> str:
    query = args["search_query"]
    reminder_minutes = [d * 1440 for d in args.get("reminder_offsets_days", [])]
    reminder_minutes += [h * 60 for h in args.get("reminder_offsets_hours", [])]

    events = await search_future_events(service, query, scope=args.get("scope", "future"))
    if not events:
        return f"no upcoming events found with {query} in them"

    updated = await update_event_reminders(service, events, reminder_minutes)
    labels = _fmt_reminder_list(args)
    return f"updated {updated} event(s) matching {query}, reminders set {labels}"


def _fmt_time(hhmm: str) -> str:
    try:
        h, m = map(int, hhmm.split(":"))
        suffix = "am" if h < 12 else "pm"
        return f"{h % 12 or 12}:{m:02d}{suffix}"
    except Exception:
        return hhmm


def _fmt_reminder(minutes: int) -> str:
    if minutes >= 1440: return f"{minutes // 1440}d"
    if minutes >= 60:   return f"{minutes // 60}hr"
    return f"{minutes}min"


def _fmt_reminder_list(args: dict) -> str:
    parts = [f"{d} day{'s' if d > 1 else ''} before" for d in args.get("reminder_offsets_days", [])]
    parts += [f"{h} hour{'s' if h > 1 else ''} before" for h in args.get("reminder_offsets_hours", [])]
    return " + ".join(parts) if parts else "as specified"
