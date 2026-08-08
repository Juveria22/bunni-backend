"""
Claude agent. Takes a user message + their calendar service,
routes to the right tool, executes it, returns a reply in gen-z tone.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import asyncio

import anthropic

from services.calendar import (
    create_event,
    search_future_events,
    update_event_reminders,
    update_event_time,
    find_conflicting_events,
    parse_event_window,
    all_day_window,
    read_event_window,
    now_local,
)

# Google's own default reminder for all-day events: 9am the day before.
# The normal 60-minute default would fire at 11pm the previous night.
ALL_DAY_REMINDER_MINUTES = 900

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
                "start_time":             {"type": "string", "description": "HH:MM 24hr. Omit entirely for an all day event."},
                "duration_minutes":       {"type": "integer", "default": 60},
                "location":               {"type": "string"},
                "reminder_minutes_before":{"type": "integer", "default": 60},
                "description":            {"type": "string"},
                "all_day": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "True for an event with no specific time that should sit at the top "
                        "of the day in google calendar. Leave start_time out when this is true. "
                        "Never use 00:00 to mean all day."
                    ),
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "YYYY-MM-DD, the LAST day of a multi day all day event such as a trip "
                        "or vacation. Only meaningful when all_day is true."
                    ),
                },
            },
            "required": ["title", "date"],
        },
    },
    {
        "name": "reschedule_event",
        "description": (
            "Use when the user wants to MOVE or CHANGE THE TIME or DATE of an event that "
            "ALREADY EXISTS. e.g. 'move my dentist appt to 3pm', 'change the meeting to friday', "
            "'make my trip all day instead'. Never use create_calendar_event for this, that "
            "would leave them with two copies of the event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search_query":    {"type": "string", "description": "Words identifying the existing event, e.g. 'dentist'"},
                "new_date":        {"type": "string", "description": "YYYY-MM-DD. Omit to keep the current date."},
                "new_start_time":  {"type": "string", "description": "HH:MM 24hr. Omit to keep the current time."},
                "duration_minutes":{"type": "integer", "description": "Omit to keep the current length."},
                "all_day":         {"type": "boolean", "default": False, "description": "True to convert it to an all day event."},
            },
            "required": ["search_query"],
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

you have four tools. use exactly one, unless the user is calling off something you just asked them about, then reply in plain text with no tool:
1. create_calendar_event - for a NEW event
2. reschedule_event - to move an event that ALREADY exists to a different time or date
3. update_reminders_by_query - for adding/changing reminders on existing events
4. ask_clarification - only if you genuinely cannot proceed without more info

new vs existing (important):
- words like move, change, reschedule, push, make it, shift, instead all mean an EXISTING event. use reschedule_event
- creating a second copy of something they already have is always wrong. if they are talking about an event they already told you about, move it, do not add it
- only use create_calendar_event when the event does not exist yet

all day events:
- if they say all day, or the thing has no natural time (birthday, holiday, vacation, trip, deadline, day off), set all_day true and leave start_time out entirely
- never use 00:00 or midnight to mean all day, that creates a real event at 12am which is wrong
- for a range like "vacation monday to friday" set all_day true, date is the first day, end_date is the last day

rules:
- if a nj city is mentioned without a state, append ", nj" to the location
- "week before and day before" → reminder_offsets_days: [7, 1]
- be confident, if intent is clear act on it

double booking:
- clashes are handled outside of you. the calendar is checked automatically before anything is created or moved
- a reply in the history like "u already have x then, still want me to add y" was that automatic warning, and nothing was created that turn
- you never need to check for or ask about clashes yourself, just make the call

conversation context:
- you can see the recent messages in this thread, use them
- if you asked for missing info last turn, the new message is the answer. combine it with what they already told you and act, do not ask again
- follow ups like "make it 3pm instead" or "same time next week" refer to the event from the previous turns
- if the new message is clearly a fresh request, ignore the older turns

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
  "u already have standup 3:00pm to 3:30pm then, still want dentist added"
  "couldn't find any events with that name\""""


@dataclass
class AgentReply:
    """
    What the agent produced. `pending_action` is set only when a create or a
    reschedule was blocked by a clash — it carries everything needed to replay
    the action if the user says yes, so the confirmation never depends on the
    model's judgment.
    """
    text: str
    pending_action: dict | None = None


# Whole-message matches only. "yes but make it 4pm" is deliberately NOT a
# confirmation — it falls through to the agent as a fresh request, which
# re-checks the new slot. Better to re-ask than to book the wrong thing.
_CONFIRM_REPLIES = {
    "y", "ye", "yes", "yea", "yeah", "yep", "yup", "sure", "ok", "okay", "k",
    "fine", "confirm", "confirmed", "do it", "add it", "still add it", "add",
    "anyway", "go ahead", "book it", "schedule it", "yes please", "yes pls",
}

_DECLINE_REPLIES = {
    "n", "no", "nah", "nope", "nevermind", "never mind", "nvm", "cancel",
    "skip", "skip it", "dont", "don't", "do not", "forget it", "leave it",
    "no thanks", "no thx",
}


def classify_confirmation(text: str) -> str:
    """
    Read a reply to a clash warning as "confirm", "decline", or "unrelated".
    Plain string matching, no model call — this decides whether we write to
    someone's calendar, so it should be predictable and cheap.
    """
    cleaned = text.strip().lower().strip(".!?,")
    if cleaned in _CONFIRM_REPLIES:
        return "confirm"
    if cleaned in _DECLINE_REPLIES:
        return "decline"
    return "unrelated"


async def perform_confirmed_action(record: dict, service) -> str:
    """
    Carry out an action the user confirmed after a clash warning. The check is
    skipped here because we already told them exactly what it clashes with.
    """
    args = record.get("args", record)  # bare args = an older create-only record

    if record.get("action") == "reschedule":
        return (await _apply_reschedule(
            args, record["event"], service, skip_conflict_check=True
        )).text

    return (await _handle_create(args, service, skip_conflict_check=True)).text


def build_system_prompt() -> list[dict]:
    """
    Returns a system prompt as a list of content blocks.
    The large static block is marked for caching. Anthropic's API
    will cache it for ~5 minutes, so repeat callers skip reprocessing it.
    The tiny date block is uncached since it changes daily.
    """
    # Must be local, not utc — after 8pm eastern utc is already tomorrow, which
    # made the model resolve "tomorrow" to the day after
    today = now_local().strftime('%A, %B %d, %Y')
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


async def run_agent(
    user_message: str,
    calendar_service,
    history: list[dict] | None = None,
) -> AgentReply:
    """
    history is prior turns for this user, oldest first, already in
    Anthropic message shape. Stored turns are plain text only — tool calls
    are collapsed into the reply we texted back, so there are no dangling
    tool_use blocks to pair with tool_results.
    """
    messages = [*(history or []), {"role": "user", "content": user_message}]

    response = await asyncio.wait_for(client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=build_system_prompt(),
        tools=TOOLS,
        messages=messages,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    ), timeout=10.0)

    # log cache hit/miss so you can verify it's working
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_created = getattr(usage, "cache_creation_input_tokens", 0)
    logger.info(f"tokens | in:{usage.input_tokens} out:{usage.output_tokens} cache_read:{cache_read} cache_created:{cache_created}")

    if response.stop_reason != "tool_use":
        text = next((b.text for b in response.content if b.type == "text"), None)
        return AgentReply(text or "idk what you mean, try again")

    tool = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool:
        return AgentReply("something broke try again")

    logger.info(f"Tool: {tool.name} | Input: {json.dumps(tool.input, indent=2)}")

    if tool.name == "create_calendar_event":
        return await _handle_create(tool.input, calendar_service)
    elif tool.name == "reschedule_event":
        return await _handle_reschedule(tool.input, calendar_service)
    elif tool.name == "update_reminders_by_query":
        return AgentReply(await _handle_update_reminders(tool.input, calendar_service))
    elif tool.name == "ask_clarification":
        return AgentReply(tool.input["question"].lower())

    return AgentReply("something went wrong try again")


def _is_all_day(args: dict) -> bool:
    """
    A missing start_time means all day. The model is told never to send 00:00
    for this, but treating no-time as all-day also stops a missing field from
    silently becoming a midnight event.
    """
    return bool(args.get("all_day")) or not args.get("start_time")


async def _handle_create(args: dict, service, skip_conflict_check: bool = False) -> AgentReply:
    all_day = _is_all_day(args)
    duration = args.get("duration_minutes", 60)
    default_reminder = ALL_DAY_REMINDER_MINUTES if all_day else 60
    reminder_minutes = args.get("reminder_minutes_before", default_reminder)

    # Check what's already there before writing. On a clash nothing is created —
    # we hand back a question plus the args, and the caller parks them until the
    # user answers. skip_conflict_check is internal only, never model-controlled.
    if not skip_conflict_check:
        if all_day:
            start_dt, end_dt = all_day_window(args["date"], args.get("end_date"))
        else:
            start_dt, end_dt = parse_event_window(args["date"], args["start_time"], duration)

        conflicts = await find_conflicting_events(service, start_dt, end_dt)
        if conflicts:
            logger.info(f"Conflict on {args['title']}: {[c.get('summary') for c in conflicts]}")
            return AgentReply(
                _fmt_conflict_warning(args["title"], conflicts, multi_day=all_day),
                pending_action={"action": "create", "args": args},
            )

    await create_event(
        service,
        title=args["title"],
        date=args["date"],
        start_time=args.get("start_time"),
        duration_minutes=duration,
        location=args.get("location"),
        reminder_minutes=reminder_minutes,
        description=args.get("description"),
        all_day=all_day,
        end_date=args.get("end_date"),
    )

    loc = f" at {args['location']}" if args.get("location") else ""

    if all_day:
        when = _fmt_day_span(args["date"], args.get("end_date"))
        return AgentReply(
            f"added {args['title']} {when} all day{loc}, "
            f"reminder {_fmt_all_day_reminder(reminder_minutes)}"
        )

    return AgentReply(
        f"added {args['title']} on {args['date']} at {_fmt_time(args['start_time'])}{loc}, "
        f"reminder {_fmt_reminder(reminder_minutes)} before"
    )


async def _handle_reschedule(args: dict, service) -> AgentReply:
    """
    Move an event that already exists. Looks it up first so we never create a
    second copy of something the user already has.
    """
    query = args["search_query"]
    matches = await search_future_events(service, query, scope="future")

    if not matches:
        return AgentReply(f"couldn't find any upcoming events matching {query}")

    if len(matches) > 1:
        options = ", ".join(_fmt_event_span(e, with_date=True) for e in matches[:3])
        extra = f" and {len(matches) - 3} more" if len(matches) > 3 else ""
        return AgentReply(f"found a few matching {query}: {options}{extra}, which one")

    return await _apply_reschedule(args, matches[0], service)


async def _apply_reschedule(
    args: dict,
    event: dict,
    service,
    skip_conflict_check: bool = False,
) -> AgentReply:
    """Apply the move, keeping whatever the user didn't ask to change."""
    cur_date, cur_time, cur_duration, was_all_day = read_event_window(event)

    new_date = args.get("new_date") or cur_date
    new_time = args.get("new_start_time") or (None if args.get("all_day") else cur_time)
    duration = args.get("duration_minutes") or cur_duration or 60
    # Stays all day unless they gave it a time; becomes all day if they asked
    all_day = bool(args.get("all_day")) or (was_all_day and not args.get("new_start_time"))
    title = event.get("summary") or "that"

    if not skip_conflict_check:
        if all_day:
            start_dt, end_dt = all_day_window(new_date)
        else:
            start_dt, end_dt = parse_event_window(new_date, new_time, duration)

        # Excluding this event's own id — otherwise moving it an hour later
        # would report a clash with itself
        conflicts = await find_conflicting_events(
            service, start_dt, end_dt, exclude_event_ids={event.get("id")}
        )
        if conflicts:
            logger.info(f"Conflict moving {title}: {[c.get('summary') for c in conflicts]}")
            return AgentReply(
                _fmt_conflict_warning(title, conflicts, multi_day=all_day, moving=True),
                pending_action={"action": "reschedule", "args": args, "event": event},
            )

    await update_event_time(
        service,
        event_id=event["id"],
        date=new_date,
        start_time=new_time,
        duration_minutes=duration,
        all_day=all_day,
    )

    if all_day:
        return AgentReply(f"moved {title.lower()} to {new_date} all day")
    return AgentReply(f"moved {title.lower()} to {new_date} at {_fmt_time(new_time)}")


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


def _fmt_conflict_warning(
    title: str,
    conflicts: list[dict],
    multi_day: bool = False,
    moving: bool = False,
) -> str:
    """
    The clash question we text back. Every clashing event is named — a partial
    list would let someone confirm a booking over something they never saw.
    """
    labels = [_fmt_event_span(e, with_date=multi_day) for e in conflicts]
    verb = "move it there anyway" if moving else f"still want me to add {title.lower()}"

    if len(labels) == 1:
        return f"u already have {labels[0]} then, {verb}"

    # Same title means they probably meant to move it, not have two of it
    same_name = any(
        (e.get("summary") or "").strip().lower() == title.strip().lower()
        for e in conflicts
    )
    dupe = ", adding this gives u two of them" if same_name and not moving else ""

    return (
        f"u already have {len(labels)} things then: {', '.join(labels)}{dupe}, {verb}"
    )


def _fmt_event_span(event: dict, with_date: bool = False) -> str:
    """"standup 3:00pm to 3:30pm" for an existing calendar event."""
    summary = (event.get("summary") or "untitled").lower()
    try:
        start = datetime.fromisoformat(event["start"]["dateTime"])
        end = datetime.fromisoformat(event["end"]["dateTime"])
        day = f"{start.strftime('%b %d').lower()} " if with_date else ""
        return (
            f"{day}{summary} {_fmt_time(f'{start.hour:02d}:{start.minute:02d}')} "
            f"to {_fmt_time(f'{end.hour:02d}:{end.minute:02d}')}"
        )
    except (KeyError, ValueError):
        return summary


def _fmt_day_span(date: str, end_date: str | None) -> str:
    """"on 2026-08-11" or "2026-08-11 to 2026-08-15" for an all-day event."""
    if end_date and end_date != date:
        return f"{date} to {end_date}"
    return f"on {date}"


def _fmt_all_day_reminder(minutes: int) -> str:
    """
    All-day reminders are offsets from midnight, so "15hr before" is a useless
    thing to text someone. Say when it actually fires.
    """
    if minutes == ALL_DAY_REMINDER_MINUTES:
        return "9am the day before"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "on the day" if days == 0 else f"{days} day{'s' if days > 1 else ''} before"
    return f"{_fmt_reminder(minutes)} before"


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
