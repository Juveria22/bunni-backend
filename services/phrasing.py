"""
calendar data to the words we text back

split out of the agent because none of it involves the model, pure string
building, and it is where the sms segment count actually gets decided
"""

from datetime import datetime

from services.calendar import ALL_DAY_REMINDER_MINUTES
from services.sanitize import scrub

# how many clashes get named in the warning, all of them are still counted
# a week long all day event collides with a whole working week, naming forty is
# a five segment sms, more than the model call that produced it
MAX_CONFLICTS_NAMED = 3


def fmt_conflict_warning(
    title: str,
    conflicts: list[dict],
    multi_day: bool = False,
    moving: bool = False,
) -> str:
    """
    the clash question we text back

    the count is always true, a count that lied would let someone confirm a
    booking over more than they realised. only the first few get named
    """
    verb = "move it there anyway?" if moving else f"still want me to add {title.lower()}?"

    if len(conflicts) == 1:
        # room to spell it out properly when there is only one
        return f"u already have {fmt_event_span(conflicts[0], with_date=multi_day)} then, {verb}"

    # start times only past that, end times are what tip a long list over a
    # segment and a start time is enough to recognise your own calendar
    named = conflicts[:MAX_CONFLICTS_NAMED]
    labels = [fmt_event_span(e, with_date=multi_day, compact=True) for e in named]
    rest = len(conflicts) - len(named)
    more = f" and {rest} more" if rest else ""

    # same title means they probably meant to move it, not have two
    same_name = any(
        scrub(e.get("summary")).strip().lower() == title.strip().lower()
        for e in conflicts
    )
    dupe = ", adding this gives u two of them" if same_name and not moving else ""

    return f"u already have {len(conflicts)} things then: {', '.join(labels)}{more}{dupe}, {verb}"


def fmt_event_span(event: dict, with_date: bool = False, compact: bool = False) -> str:
    """
    standup 3:00pm to 3:30pm, or standup 3:00pm when compact
    all day events say so instead of a time
    """
    # scrubbed because this goes out as sms, a hostile title could carry
    # newlines or enough length to cost several segments
    summary = (scrub(event.get("summary")) or "untitled").lower()
    start_raw = event.get("start", {})

    if "dateTime" not in start_raw:
        day = start_raw.get("date")
        return f"{summary} on {day} all day" if with_date and day else f"{summary} all day"

    try:
        start = datetime.fromisoformat(start_raw["dateTime"])
        day = f"{start.strftime('%b %d').lower()} " if with_date else ""
        opens = fmt_time(f"{start.hour:02d}:{start.minute:02d}")

        if compact:
            return f"{day}{summary} {opens}"

        end = datetime.fromisoformat(event["end"]["dateTime"])
        closes = fmt_time(f"{end.hour:02d}:{end.minute:02d}")
        return f"{day}{summary} {opens} to {closes}"
    except (KeyError, ValueError):
        return summary


def fmt_day_span(date: str, end_date: str | None) -> str:
    """on 2026-08-11, or 2026-08-11 to 2026-08-15 for a multi day one"""
    if end_date and end_date != date:
        return f"{date} to {end_date}"
    return f"on {date}"


def fmt_all_day_reminder(minutes: int) -> str:
    """
    all day reminders are offsets from midnight, so "15hr before" is useless to
    text someone. say when it actually fires
    """
    if minutes == ALL_DAY_REMINDER_MINUTES:
        return "9am the day before"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "on the day" if days == 0 else f"{days} day{'s' if days > 1 else ''} before"
    return f"{fmt_reminder(minutes)} before"


# a confirmation naming eight addresses is several segments, and the count is
# what actually answers "who am i inviting"
MAX_GUESTS_NAMED = 2


def fmt_guest_list(emails: list[str]) -> str:
    """
    jake@x.com, or jake@x.com and 2 others

    addresses are long and this goes into a yes/no question, so past a couple
    the rest are counted. the count is always true, naming two of five and
    saying nothing about the rest would let someone confirm a wider invite than
    they realised
    """
    named = [scrub(e, limit=60) for e in emails[:MAX_GUESTS_NAMED]]
    rest = len(emails) - len(named)

    if not named:
        return "them"
    if rest:
        return f"{', '.join(named)} and {rest} other{'s' if rest > 1 else ''}"
    if len(named) == 2:
        return f"{named[0]} and {named[1]}"
    return named[0]


def fmt_detail_changes(changes: dict) -> str:
    """
    the "what changed" tail of an update reply, in a fixed order

    only the keys present are mentioned, so someone who asked for one thing is
    not read a list of five. a cleared field says so rather than going quiet,
    "removed" and "unchanged" look identical from the outside otherwise
    """
    parts = []

    if "location" in changes:
        # scrubbed like every other string that ends up in an sms, a pasted
        # address can carry newlines and this is the only place it goes out
        where = scrub(changes["location"])
        parts.append(f"location now {where}" if where else "location removed")

    if "description" in changes:
        parts.append("note updated" if changes["description"] else "note removed")

    if "color" in changes:
        # the word they used, not google's name for it, "color now tomato" when
        # they asked for red reads like it did something else
        parts.append(f"color now {changes['color']}")

    if "busy" in changes:
        parts.append(
            "marked busy" if changes["busy"]
            else "marked free so it wont block new stuff"
        )

    if "visibility" in changes:
        visibility = changes["visibility"]
        parts.append(
            "set to private" if visibility == "private"
            else "set to public" if visibility == "public"
            else "visibility back to default"
        )

    return ", ".join(parts)


def fmt_time(hhmm: str) -> str:
    """15:30 -> 3:30pm, returns the input unchanged if it will not parse"""
    try:
        h, m = map(int, hhmm.split(":"))
        suffix = "am" if h < 12 else "pm"
        return f"{h % 12 or 12}:{m:02d}{suffix}"
    except Exception:
        return hhmm


def fmt_reminder(minutes: int) -> str:
    """compact form for the tail of a confirmation, 1hr / 2d / 30min"""
    if minutes >= 1440: return f"{minutes // 1440}d"
    if minutes >= 60:   return f"{minutes // 60}hr"
    return f"{minutes}min"


def fmt_applied_reminders(applied_minutes: list[int]) -> str:
    """
    spelled out form for offsets that made it onto an event
    ordered how the user experiences them, furthest out first
    """
    parts = []
    for minutes in sorted(applied_minutes, reverse=True):
        if minutes and minutes % 1440 == 0:
            days = minutes // 1440
            parts.append(f"{days} day{'s' if days > 1 else ''} before")
        elif minutes and minutes % 60 == 0:
            hours = minutes // 60
            parts.append(f"{hours} hour{'s' if hours > 1 else ''} before")
        else:
            parts.append(f"{minutes} min before")
    return " + ".join(parts) if parts else "as specified"
