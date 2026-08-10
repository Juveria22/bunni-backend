"""
Outbound messaging helper, supports both SMS and WhatsApp via Twilio.
"""

import asyncio
import logging
import os
from twilio.rest import Client

logger = logging.getLogger(__name__)

_twilio = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
TWILIO_SMS_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
TWILIO_WA_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")  # optional


# A single character outside GSM-7 forces the whole SMS into UCS-2, which cuts
# the segment size from 160 characters to 70 — one stray curly apostrophe can
# double or triple what a message costs. These are the ones a language model
# actually emits; anything else non-ascii is left alone rather than mangled.
_GSM_SUBSTITUTIONS = str.maketrans({
    "‘": "'", "’": "'",           # curly single quotes
    "“": '"', "”": '"',           # curly double quotes
    "–": "-", "—": "-",           # en dash, em dash
    "…": "...",                        # ellipsis
    " ": " ",                          # non-breaking space
    "•": "*",                          # bullet
})


def to_gsm7(body: str) -> str:
    """Swap the common non-GSM characters for plain equivalents."""
    return body.translate(_GSM_SUBSTITUTIONS)


async def send_message(to: str, body: str, channel: str = "sms") -> None:
    """
    Send an outbound message via SMS or WhatsApp.
    channel: "sms" | "whatsapp"

    Twilio's client is blocking, so it goes to a thread — this is called from
    a background task while the server is free to handle other webhooks.
    """
    body = to_gsm7(body)

    if channel == "whatsapp" and TWILIO_WA_NUMBER:
        kwargs = {"to": f"whatsapp:{to}", "from_": TWILIO_WA_NUMBER, "body": body}
    else:
        kwargs = {"to": to, "from_": TWILIO_SMS_NUMBER, "body": body}

    await asyncio.to_thread(lambda: _twilio.messages.create(**kwargs))
    logger.info(f"Sent {channel} reply to {to} ({len(body)} chars)")

# Convenience alias, defaults to SMS so existing callers don't break
async def send_sms(to: str, body: str) -> None:
    await send_message(to, body, channel="sms")