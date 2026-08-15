"""
outbound messaging via twilio, sms and whatsapp
last stop before the wire, so it owns character substitution and length caps
"""

import asyncio
import logging
import os
from twilio.rest import Client

from services.budget import record_sms

logger = logging.getLogger(__name__)

_twilio = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
TWILIO_SMS_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
TWILIO_WA_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")  # optional

# a lone sms fits 160 chars, but a split one spends 6 bytes per part on the
# concatenation header, so every segment past the first holds 153, not 160
SINGLE_SEGMENT_CHARS = 160
CONCAT_SEGMENT_CHARS = 153

# two gsm-7 segments. 306 = 2 x 153, the old 320 sat 14 chars into a third, so
# a trimmed reply cost 3 segments, half again what the cap was there to buy
#
# callers are supposed to send one or two lines, but "supposed to" is a prompt
# and the agent runs at max_tokens=1024, ~4000 chars or 25 billable segments if
# a reply runs away. last line of defence in front of the bill, and the log line
# says when a prompt has drifted
MAX_SMS_CHARS = 306

# whatsapp bills per conversation window not per segment, so length is free
# still bounded, a reply that long is a bug worth seeing not delivering
MAX_WHATSAPP_CHARS = 1000


# one character outside gsm-7 forces the whole sms into ucs-2 and cuts the
# segment from 160 chars to 70, a stray curly apostrophe can triple the cost
# these are the ones a model actually emits, other non ascii is left alone
# rather than mangled
#
# the invisible ones are written as escapes on purpose. this table used to
# carry a plain ascii space where the non-breaking space was meant to go, which
# reads identically in an editor and quietly did nothing, so a single nbsp in a
# reply still dropped the whole message into ucs-2 and cut the segment from 153
# characters to 67
_GSM_SUBSTITUTIONS = str.maketrans({
    "‘": "'", "’": "'",       # curly single quotes
    "“": '"', "”": '"',       # curly double quotes
    "–": "-", "—": "-",       # en dash, em dash
    "−": "-",                      # minus sign
    "…": "...",                    # ellipsis
    " ": " ",                      # non-breaking space
    " ": " ",                      # narrow no-break space
    "​": "",                       # zero width space
    "•": "*",                      # bullet
})


def to_gsm7(body: str) -> str:
    """swap the common non gsm characters for plain equivalents"""
    return body.translate(_GSM_SUBSTITUTIONS)


def segment_count(body: str) -> int:
    """
    how many gsm-7 segments this body bills as

    a lone sms holds 160, a split one 153 per part, so the count jumps at 161
    and every 153 after
    """
    length = len(body)
    if length == 0:
        return 0
    if length <= SINGLE_SEGMENT_CHARS:
        return 1
    return -(-length // CONCAT_SEGMENT_CHARS)


def clamp(body: str, channel: str = "sms") -> str:
    """trim a reply to something that costs what we expect"""
    limit = MAX_WHATSAPP_CHARS if channel == "whatsapp" else MAX_SMS_CHARS
    if len(body) <= limit:
        return body
    logger.warning(f"Reply was {len(body)} chars, trimming to {limit} ({channel})")
    # plain dots not "…", that character is outside gsm-7 and would drag the
    # message into ucs-2, halving the segment size we just trimmed to fit
    return body[: limit - 3].rstrip() + "..."


async def send_message(to: str, body: str, channel: str = "sms") -> None:
    """
    send one outbound message, channel is "sms" | "whatsapp"

    twilio's client is blocking so it goes to a thread, this runs from a
    background task while the server handles other webhooks
    """
    # substitute then clamp, the substitutions change length so clamping after
    # is what actually bounds the segment count
    body = clamp(to_gsm7(body), channel)

    if channel == "whatsapp" and TWILIO_WA_NUMBER:
        kwargs = {"to": f"whatsapp:{to}", "from_": TWILIO_WA_NUMBER, "body": body}
    else:
        kwargs = {"to": to, "from_": TWILIO_SMS_NUMBER, "body": body}

    await asyncio.to_thread(lambda: _twilio.messages.create(**kwargs))

    segments = segment_count(body)
    await record_sms(segments)
    logger.info(f"Sent {channel} reply ({len(body)} chars, {segments} segment(s))")

# alias, defaults to sms so existing callers keep working
async def send_sms(to: str, body: str) -> None:
    await send_message(to, body, channel="sms")
