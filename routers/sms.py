"""
Unified messaging router, handles both SMS and WhatsApp webhooks from Twilio.
Same logic for both channels. WhatsApp is significantly cheaper at scale
($0.02/conversation window vs $0.0079/segment for SMS).

To enable WhatsApp:
  1. Apply for Twilio WhatsApp Business API (or use sandbox for testing)
  2. Set TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886 in env
  3. Point your WhatsApp webhook at /message (same endpoint as SMS)

The From field tells us which channel: "whatsapp:+1..." vs "+1..."
"""

import os
import logging
from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

from db.session import get_db
from db.repo import get_or_create_user
from services.google_oauth import generate_auth_url, get_calendar_service_for_user
from services.agent import run_agent
from services.rate_limit import check_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()

WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")  # "whatsapp:+14155238886"


def _parse_channel(from_field: str) -> tuple[str, str]:
    """
    Returns (phone, channel) where channel is whatsapp or sms.
    Twilio prefixes WhatsApp senders with whatsapp:
    """
    if from_field.startswith("whatsapp:"):
        return from_field.replace("whatsapp:", ""), "whatsapp"
    return from_field, "sms"


def _make_response(body: str, channel: str, to_number: str) -> MessagingResponse:
    """Build a TwiML response. For WhatsApp, prefix the To number."""
    resp = MessagingResponse()
    msg = resp.message(body)
    if channel == "whatsapp" and WHATSAPP_NUMBER:
        msg.sender = WHATSAPP_NUMBER
    return resp

ONBOARDING_MSG = (
    "hey "
    "connect ur gcal to get started: {auth_url}"
)

ALREADY_ONBOARDED_MSG = (
    "u're connected just txt me what you want on ur gcal"
)

@router.get("/test")
async def test():
    return {"status": "ok"}

@router.post("/message", response_class=PlainTextResponse)
async def receive_message(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
):
    """
    Unified endpoint for both SMS and WhatsApp.
    Point both Twilio webhooks here.
    """
    phone, channel = _parse_channel(From.strip())
    text = Body.strip()

    logger.info(f"[{channel}] {phone}: {text!r}")

    # Rate limit: 30 messages per user per hour (Redis-backed)
    if not await check_rate_limit(phone):
        resp = _make_response("one at a time bestie", channel, phone)
        return PlainTextResponse(str(resp), media_type="application/xml")

    async with get_db() as db:
        user, created = await get_or_create_user(db, phone)

        # New user, send onboarding link
        if not user.is_onboarded:
            auth_url = generate_auth_url(phone)
            msg = ONBOARDING_MSG.format(auth_url=auth_url)
            resp = _make_response(msg, channel, phone)
            logger.info(f"Sent onboarding link to {phone} via {channel}")
            return PlainTextResponse(str(resp), media_type="application/xml")

        # Returning user texting connect or reconnect, re-auth
        if text.lower() in ("connect", "reconnect", "reauth", "reset"):
            auth_url = generate_auth_url(phone)
            resp = _make_response(f"no problem here's a fresh link: {auth_url}", channel, phone)
            return PlainTextResponse(str(resp), media_type="application/xml")

        # Known user, run the agent with their calendar service
        try:
            calendar_service = get_calendar_service_for_user(user.google_refresh_token)
            reply = await run_agent(text, calendar_service)
        except Exception as e:
            logger.exception(f"Agent error for {phone}: {e}")
            if "invalid_grant" in str(e).lower() or "token" in str(e).lower():
                auth_url = generate_auth_url(phone)
                reply = f"your google connection expired reconnect here: {auth_url}"
            else:
                reply = "sumn went wrong try again in a sec"

    resp = _make_response(reply, channel, phone)
    return PlainTextResponse(str(resp), media_type="application/xml")


# Keep /sms as an alias so existing Twilio configs don't break
@router.post("/sms", response_class=PlainTextResponse)
async def receive_sms(request: Request, From: str = Form(...), Body: str = Form(...)):
    return await receive_message(request, From=From, Body=Body)
