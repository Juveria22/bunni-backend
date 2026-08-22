"""
untrusted text handling

event titles and locations are written by whoever made the event, and anyone can
put an event on a primary calendar by sending an invite, so treat them as
attacker controlled. they land in the two worst places: tool results, sitting
next to real instructions, and the body of a text from a number the user trusts

two defences, deliberately separate
scrub always runs and makes a string structurally harmless
looks_like_injection only adds friction, a false positive costs one confirmation
and a false negative costs a calendar
"""

import re

# long enough for a real event title, short enough that a paragraph of injected
# instructions cannot survive the trip
MAX_UNTRUSTED_CHARS = 120

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_RUNS = re.compile(r"\s{2,}")

_INJECTION_MARKERS = re.compile(
    r"""(
        ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)
      | disregard\s+(the\s+)?(above|previous|prior|earlier)
      | (system|assistant|developer)\s*(prompt|message|instructions?)
      | you\s+(are|must|should|will)\s+now
      | new\s+instructions?
      | </?\s*(system|instructions?|important|prompt)\s*>
      | https?://
      | \+?\d[\d\s().\-]{8,}\d
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def scrub(value: str | None, limit: int = MAX_UNTRUSTED_CHARS) -> str:
    """
    flatten a calendar string into something safe to embed anywhere

    newlines are the main thing to kill, they let injected text impersonate a
    new section of the prompt. the length cap stops a title carrying a paragraph,
    which also keeps one clash warning from becoming six sms segments
    """
    if not value:
        return ""
    return _RUNS.sub(" ", _CONTROL.sub(" ", value)).strip()[:limit]


def mask_phone(phone: str | None) -> str:
    """
    a phone number cut down to what a person needs to recognise it, no more

    used for logs, which have no retention window and sit outside STOP, and for
    the oauth success page, where the point is telling at a glance whether the
    number is yours. plain ascii so it survives a windows console
    """
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:2]}***{phone[-4:]}"


def looks_like_injection(*values: str | None) -> bool:
    """
    True when one of these reads like an instruction or a lure rather than the
    name of something on a calendar

    adds a confirmation step, never silently drops data
    getting it wrong in the cautious direction is cheap
    """
    return any(_INJECTION_MARKERS.search(v) for v in values if v)
