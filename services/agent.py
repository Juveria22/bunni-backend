"""
Claude agent. Runs a tool loop over the user's calendar: the model can read
what's actually there, then act on real event ids. Replies in gen-z tone.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
import asyncio

import anthropic

from services.calendar import (
    create_event,
    delete_event,
    get_event,
    list_events,
    summarize_event,
    update_event_reminders,
    update_event_time,
    find_conflicting_events,
    parse_event_window,
    all_day_window,
    read_event_window,
    now_local,
    TIMEZONE,
)

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

# Google's own default reminder for all-day events: 9am the day before.
# The normal 60-minute default would fire at 11pm the previous night.
ALL_DAY_REMINDER_MINUTES = 900

# Replies go out over the rest api after the webhook has returned, so Twilio's
# ~15s timeout no longer bounds this. The cap is now about not leaving someone
# staring at their phone, and about not looping forever on a bad day.
# 4 calls is enough to search, widen the search once, then act.
AGENT_DEADLINE_SECONDS = 30.0
MAX_MODEL_CALLS = 4

TOOLS = [
    {
        "name": "find_events",
        "description": (
            "Read what is actually on the calendar. Call this FIRST whenever the user "
            "refers to an event that already exists — moving it, deleting it, cancelling it, "
            "changing its reminders — or when you need to know what a day looks like. "
            "Returns events with their ids, which the other tools need."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD, start of the window to look at. Defaults to today."},
                "date_to":   {"type": "string", "description": "YYYY-MM-DD, end of the window. Defaults to 60 days out."},
                "query":     {"type": "string", "description": "Optional keyword. Leave it out unless you are confident of the exact wording, a date range alone is usually better."},
            },
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Add a NEW event. Only for something that does not exist yet — never use this "
            "to change an event the user already has."
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
                        "True for an event with no specific time that sits at the top of the day. "
                        "Leave start_time out when true. Never use 00:00 to mean all day."
                    ),
                },
                "end_date": {"type": "string", "description": "YYYY-MM-DD, LAST day of a multi day all day event. Only with all_day."},
            },
            "required": ["title", "date"],
        },
    },
    {
        "name": "reschedule_event",
        "description": (
            "Move an existing event to a different time or date. Needs an event_id from "
            "find_events. Anything you leave out stays as it is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id":        {"type": "string", "description": "id from find_events"},
                "new_date":        {"type": "string", "description": "YYYY-MM-DD. Omit to keep the current date."},
                "new_start_time":  {"type": "string", "description": "HH:MM 24hr. Omit to keep the current time."},
                "duration_minutes":{"type": "integer", "description": "Omit to keep the current length."},
                "all_day":         {"type": "boolean", "default": False, "description": "True to turn it into an all day event."},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_event",
        "description": (
            "Delete an event off the calendar. Needs an event_id from find_events. "
            "Use for cancel, delete, remove, get rid of, call off."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "id from find_events"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "update_reminders",
        "description": "Set or change reminders on existing events. Needs event_ids from find_events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_ids":              {"type": "array", "items": {"type": "string"}},
                "reminder_offsets_days":  {"type": "array", "items": {"type": "integer"}},
                "reminder_offsets_hours": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["event_ids"],
        },
    },
]


# ---------- prompt caching ----------
# The static portion of the system prompt never changes.
# We use Anthropic's prompt caching (cache_control) so the API
# only processes it once per cache TTL (~5 min), then reads from
# a cache on subsequent calls.
# The date is injected separately as a tiny uncached block so the
# static block stays byte-for-byte identical across all users/requests.

_STATIC_PROMPT = """you are a personal ai calendar assistant. the user texts you and you manage their google calendar.

the point of you: managing a calendar from your phone is annoying. the user should be able to text you something short and sloppy and have it just be handled. you do the careful part, they do the bare minimum. do not make them repeat themselves, spell things exactly, or answer questions you could have worked out yourself

how to work:
- you can read the calendar with find_events. use it, do not guess
- if the user mentions an event that already exists, call find_events first, then act on the id it gives you
- never invent an event id. ids only come from find_events
- prefer a date range over a keyword. people do not type their event titles exactly, "saturdays office thing" might be an event called "Office Day"
- match on your own judgment: the title, the day, the time, whatever the user gave you. if they say "the first one" or "the 11-6 one" or "the one on saturday", they are answering a question you just asked, look at the list and pick it
- typos and half sentences are normal. "office day*" is them correcting the name of the thing they just mentioned, not a new request
- if the search window turns up nothing, widen it once before telling them you found nothing
- if one event is clearly the best match, just act on it. only ask when two or more are genuinely just as likely, and then list them with their day and time so one word answers it
- fill in the obvious: a lunch is an hour, a flight is not 15 minutes, "tonight" is this evening. do not interrogate them over details you can reasonably assume
- once you have done the action you are finished, do not call another tool

rules:
- if a nj city is mentioned without a state, append ", nj" to the location
- "week before and day before" -> reminder_offsets_days: [7, 1]
- be confident, if intent is clear act on it

all day events:
- if they say all day, or the thing has no natural time (birthday, holiday, vacation, trip, deadline, day off), set all_day true and leave start_time out entirely
- never use 00:00 or midnight to mean all day, that creates a real event at 12am which is wrong
- for a range like "vacation monday to friday" set all_day true, date is the first day, end_date is the last day

confirmations (clashes and deletes):
- these are handled outside of you, automatically, before anything is written
- a reply in the history like "u already have x then, still want me to add y" or "delete x, for real" was that automatic check, and nothing was written that turn
- you never need to check for clashes or ask before deleting yourself, just make the call and the confirmation happens on its own
- this is the one place extra friction is worth it, everything else should be effortless

tone rules (non-negotiable):
- all lowercase always
- no emojis
- no periods at the end of sentences ever
- question marks are fine, use one whenever you are actually asking something
- no em dashes or en dashes ever. replace with a comma or just end the thought
  bad: "added dentist — reminder set 1hr before"
  good: "added dentist, reminder 1hr before"
- no formal sentence structure. fragments are fine. preferred even
- gen-z casual. like texting a friend who happens to manage your calendar
- one or two lines max. this is sms not email
- examples of good replies:
  "added meeting with jake tomorrow at noon, reminder 1hr before"
  "moved saniyahs party to tomorrow at 12pm"
  "deleted office day"
  "what time tho?"
  "u already have standup 3:00pm to 3:30pm then, still want dentist added?"
  "couldn't find anything like that on ur calendar\""""


def build_system_prompt() -> list[dict]:
    """
    Returns a system prompt as a list of content blocks.
    The large static block is marked for caching. Anthropic's API
    will cache it for ~5 minutes, so repeat callers skip reprocessing it.
    The tiny date block is uncached since it changes daily.
    """
    # Must be local, not utc — after 8pm eastern utc is already tomorrow, which
    # made the model resolve "tomorrow" to the day after
    now = now_local()
    return [
        {
            "type": "text",
            "text": _STATIC_PROMPT,
            "cache_control": {"type": "ephemeral"},  # cache this block
        },
        {
            "type": "text",
            "text": f"today: {now.strftime('%A, %B %d, %Y')} (eastern time)",
            # no cache_control, tiny and date-specific, not worth caching
        },
    ]


@dataclass
class AgentReply:
    """
    What the agent produced. `pending_action` is set only when a write was
    blocked pending a yes/no — a clash, or a delete. It carries everything
    needed to replay the action, so the confirmation never depends on the
    model's judgment.
    """
    text: str
    pending_action: dict | None = None


# The obvious answers, matched instantly with no model call. Anything not in
# here goes to interpret_confirmation rather than being written off — people
# say "yuh", "bet", "go for it", and being made to repeat yourself is exactly
# the friction this whole thing exists to remove.
_CONFIRM_REPLIES = {
    "y", "ye", "yes", "yea", "yeah", "yep", "yup", "yuh", "ya", "yah", "yh",
    "sure", "ok", "okay", "kk", "k", "fine", "bet", "confirm", "confirmed",
    "do it", "add it", "still add it", "add", "anyway", "go ahead", "go for it",
    "book it", "schedule it", "yes please", "yes pls", "please", "pls",
    # answers to "delete x?". "cancel it" is deliberately absent — it reads as
    # decline when the question was "still want me to add x?"
    "delete it", "remove it", "yes delete", "delete",
}

_DECLINE_REPLIES = {
    "n", "no", "nah", "naw", "nope", "nvm", "nevermind", "never mind",
    "cancel", "skip", "skip it", "dont", "don't", "do not", "forget it",
    "leave it", "no thanks", "no thx", "keep it", "no dont", "actually no",
}

_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

_CLASSIFIER_PROMPT = """You classify a reply to a yes/no question from a calendar assistant.

Answer with exactly one word, nothing else:
confirm   - they are agreeing, they want it done
decline   - they do not want it done
unrelated - they are not answering the question, they changed the request, gave new details, or asked something else

This is SMS. Slang, typos, lowercase and one word answers are completely normal.
"yuh", "ya", "bet", "go for it", "yeye", "do it", "sure why not", "obvi" are all confirm.
"nah", "nah im good", "actually dont", "wait no" are all decline.
"yes but at 4pm", "make it friday instead", "what about tuesday" are unrelated — they changed what was being asked."""


def classify_confirmation(text: str) -> str:
    """
    The instant path: obvious yes/no with no model call. Returns "confirm",
    "decline", or "unrelated". "unrelated" here means "not obvious", not
    "not an answer" — the caller should fall through to interpret_confirmation.
    """
    cleaned = text.strip().lower().strip(".!?,")
    if cleaned in _CONFIRM_REPLIES:
        return "confirm"
    if cleaned in _DECLINE_REPLIES:
        return "decline"
    return "unrelated"


async def interpret_confirmation(question: str, reply: str) -> str:
    """
    Ask a small model to read an answer the word list didn't recognise.

    It only ever decides yes / no / neither. What gets written was already
    fixed and shown to the user when we asked, so a misread can at worst do
    the thing they were looking at, never something else. On any error this
    returns "unrelated", which routes to the normal agent — the safe default.
    """
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=_CLASSIFIER_MODEL,
                max_tokens=5,
                system=_CLASSIFIER_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Question asked: {question}\nTheir reply: {reply}",
                }],
            ),
            timeout=6.0,
        )
    except Exception as e:
        logger.warning(f"Confirmation classifier failed: {e}")
        return "unrelated"

    raw = "".join(b.text for b in response.content if b.type == "text")
    # Tolerate "Confirm." / "CONFIRM" / stray whitespace. Being fussy here just
    # pushes a real answer into "unrelated" and makes the user say it twice.
    word = re.sub(r"[^a-z]", "", raw.lower())
    decision = word if word in ("confirm", "decline", "unrelated") else "unrelated"

    if word != decision:
        logger.warning(f"Classifier returned {raw!r}, treating as unrelated")
    logger.info(f"Interpreted {reply!r} as {decision}")
    return decision


async def perform_confirmed_action(record: dict, service) -> str:
    """
    Carry out an action the user confirmed. Checks are skipped here because we
    already told them exactly what they were agreeing to.
    """
    action = record.get("action")
    args = record.get("args", record)  # bare args = an older create-only record

    if action == "reschedule":
        return (await _apply_reschedule(args, record["event"], service, skip_checks=True)).text
    if action == "delete":
        return (await _apply_delete(record["event"], service, skip_checks=True)).text
    return (await _handle_create(args, service, skip_checks=True)).text


# ---------- the loop ----------

async def run_agent(
    user_message: str,
    calendar_service,
    history: list[dict] | None = None,
) -> AgentReply:
    """
    history is prior turns for this user, oldest first, already in Anthropic
    message shape. Stored turns are plain text only — the tool blocks below
    live for one request, so nothing dangling gets persisted.

    find_events loops back into the model. Anything that writes ends the turn.
    """
    messages = [*(history or []), {"role": "user", "content": user_message}]
    deadline = monotonic() + AGENT_DEADLINE_SECONDS

    for _ in range(MAX_MODEL_CALLS):
        remaining = deadline - monotonic()
        if remaining <= 1.5:
            logger.warning("Agent ran out of time")
            return AgentReply("that one took too long, try again")

        response = await asyncio.wait_for(
            client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=build_system_prompt(),
                tools=TOOLS,
                messages=messages,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            ),
            timeout=remaining,
        )

        usage = response.usage
        logger.info(
            f"tokens | in:{usage.input_tokens} out:{usage.output_tokens} "
            f"cache_read:{getattr(usage, 'cache_read_input_tokens', 0)} "
            f"cache_created:{getattr(usage, 'cache_creation_input_tokens', 0)}"
        )

        if response.stop_reason != "tool_use":
            text = next((b.text for b in response.content if b.type == "text"), None)
            return AgentReply(text or "idk what you mean, try again")

        messages.append({"role": "assistant", "content": response.content})
        results = []

        for block in response.content:
            if block.type != "tool_use":
                continue

            logger.info(f"Tool: {block.name} | {json.dumps(block.input)}")

            if block.name == "find_events":
                found = await _handle_find(block.input, calendar_service)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(found),
                })
                continue

            # Everything else writes, and writing ends the turn
            return await _run_write_tool(block, calendar_service)

        messages.append({"role": "user", "content": results})

    return AgentReply("got a bit lost there, try saying it another way")


async def _run_write_tool(block, service) -> AgentReply:
    if block.name == "create_calendar_event":
        return await _handle_create(block.input, service)
    if block.name == "reschedule_event":
        return await _handle_reschedule(block.input, service)
    if block.name == "delete_event":
        return await _handle_delete(block.input, service)
    if block.name == "update_reminders":
        return AgentReply(await _handle_update_reminders(block.input, service))
    return AgentReply("something went wrong try again")


# ---------- reading ----------

async def _handle_find(args: dict, service) -> list[dict]:
    """
    Hand the model the actual calendar contents. No keyword filtering here —
    the model decides what matches, which is the whole point of it being here.
    """
    today = now_local().date()

    start = (
        datetime.fromisoformat(args["date_from"]).date()
        if args.get("date_from") else today
    )
    end = (
        datetime.fromisoformat(args["date_to"]).date()
        if args.get("date_to") else today + timedelta(days=60)
    )
    # A window that starts after it ends returns nothing and looks like
    # "no events" to the model
    if end < start:
        start, end = end, start

    events = await list_events(
        service,
        time_min=datetime.combine(start, datetime.min.time(), tzinfo=TIMEZONE),
        time_max=datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=TIMEZONE),
        query=args.get("query"),
    )

    summaries = [summarize_event(e) for e in events]
    logger.info(f"find_events {start}..{end} -> {len(summaries)} events")
    return summaries


# ---------- writing ----------

def _is_all_day(args: dict) -> bool:
    """
    A missing start_time means all day. The model is told never to send 00:00
    for this, but treating no-time as all-day also stops a missing field from
    silently becoming a midnight event.
    """
    return bool(args.get("all_day")) or not args.get("start_time")


async def _handle_create(args: dict, service, skip_checks: bool = False) -> AgentReply:
    all_day = _is_all_day(args)
    duration = args.get("duration_minutes", 60)
    default_reminder = ALL_DAY_REMINDER_MINUTES if all_day else 60
    reminder_minutes = args.get("reminder_minutes_before", default_reminder)

    # Check what's already there before writing. On a clash nothing is created —
    # we hand back a question plus the args, and the caller parks them until the
    # user answers. skip_checks is internal only, never model-controlled.
    if not skip_checks:
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
    event = await _load_event(args.get("event_id"), service)
    if event is None:
        return AgentReply("couldn't find that one on ur calendar anymore")
    return await _apply_reschedule(args, event, service)


async def _apply_reschedule(
    args: dict,
    event: dict,
    service,
    skip_checks: bool = False,
) -> AgentReply:
    """Apply the move, keeping whatever the user didn't ask to change."""
    cur_date, cur_time, cur_duration, was_all_day = read_event_window(event)

    new_date = args.get("new_date") or cur_date
    new_time = args.get("new_start_time") or (None if args.get("all_day") else cur_time)
    duration = args.get("duration_minutes") or cur_duration or 60
    # Stays all day unless they gave it a time; becomes all day if they asked
    all_day = bool(args.get("all_day")) or (was_all_day and not args.get("new_start_time"))
    title = event.get("summary") or "that"

    if not skip_checks:
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


async def _handle_delete(args: dict, service) -> AgentReply:
    event = await _load_event(args.get("event_id"), service)
    if event is None:
        return AgentReply("couldn't find that one on ur calendar anymore")
    return await _apply_delete(event, service)


async def _apply_delete(event: dict, service, skip_checks: bool = False) -> AgentReply:
    """
    Always asks first. Deleting can't be undone from a text message, and the
    model picking the wrong event out of find_events is exactly the failure
    this catches. The event is named back so they can see which one it is.
    """
    title = (event.get("summary") or "that").lower()

    if not skip_checks:
        return AgentReply(
            f"delete {_fmt_event_span(event, with_date=True)}?",
            pending_action={"action": "delete", "event": event},
        )

    await delete_event(service, event["id"])
    return AgentReply(f"deleted {title}")


async def _handle_update_reminders(args: dict, service) -> str:
    event_ids = args.get("event_ids") or []
    if not event_ids:
        return "couldn't tell which events u meant"

    reminder_minutes = [d * 1440 for d in args.get("reminder_offsets_days", [])]
    reminder_minutes += [h * 60 for h in args.get("reminder_offsets_hours", [])]
    if not reminder_minutes:
        return "how far ahead do u want the reminder"

    updated = await update_event_reminders(service, event_ids, reminder_minutes)
    if not updated:
        return "couldn't update those, try again"

    return f"updated {updated} event(s), reminders set {_fmt_reminder_list(args)}"


async def _load_event(event_id: str | None, service) -> dict | None:
    """Model-supplied ids can be stale — a 404 shouldn't take the whole reply down."""
    if not event_id:
        return None
    try:
        return await get_event(service, event_id)
    except Exception as e:
        logger.warning(f"Could not load event {event_id}: {e}")
        return None


# ---------- formatting ----------

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
    verb = "move it there anyway?" if moving else f"still want me to add {title.lower()}?"

    if len(labels) == 1:
        return f"u already have {labels[0]} then, {verb}"

    # Same title means they probably meant to move it, not have two of it
    same_name = any(
        (e.get("summary") or "").strip().lower() == title.strip().lower()
        for e in conflicts
    )
    dupe = ", adding this gives u two of them" if same_name and not moving else ""

    return f"u already have {len(labels)} things then: {', '.join(labels)}{dupe}, {verb}"


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
        # All-day events have no dateTime to render
        day = event.get("start", {}).get("date")
        return f"{summary} on {day} all day" if with_date and day else summary


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
