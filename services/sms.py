"""
Outbound messaging helper, supports both SMS and WhatsApp via Twilio.
"""

import os
from twilio.rest import Client

_twilio = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
TWILIO_SMS_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
TWILIO_WA_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")  # optional


async def send_message(to: str, body: str, channel: str = "sms") -> None:
    """
    Send an outbound message via SMS or WhatsApp.
    channel: "sms" | "whatsapp"
    """
    if channel == "whatsapp" and TWILIO_WA_NUMBER:
        _twilio.messages.create(
            to=f"whatsapp:{to}",
            from_=TWILIO_WA_NUMBER,
            body=body,
        )
    else:
        _twilio.messages.create(
            to=to,
            from_=TWILIO_SMS_NUMBER,
            body=body,
        )

def _make_vcard() -> str:
    number = TWILIO_PHONE_NUMBER  # add this import from services.sms or os.environ
    return f"""BEGIN:VCARD
VERSION:3.0
FN:gcal
TEL;TYPE=CELL:{number}
END:VCARD"""

# Convenience alias, defaults to SMS so existing callers don't break
async def send_sms(to: str, body: str) -> None:
    await send_message(to, body, channel="sms")