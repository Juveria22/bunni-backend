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
from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

from db.session import get_db
from db.repo import (
    get_or_create_user,
    get_recent_messages,
    save_turn,
    get_pending_event,
    set_pending_event,
    clear_pending_event,
)
from services.google_oauth import generate_auth_url, get_calendar_service_for_user
from services.agent import run_agent, classify_confirmation, perform_confirmed_action
from services.rate_limit import check_rate_limit
from services.sms import send_sms, send_message
#, send_vcard

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

def _make_vcard() -> str:
    number = TWILIO_PHONE_NUMBER  # add this import from services.sms or os.environ
    return f"""BEGIN:VCARD
VERSION:3.0
FN:gcal
TEL;TYPE=CELL:{number}
END:VCARD"""

ONBOARDING_MSG = "heyy\n\ngcal setup: {auth_url}"

ALREADY_ONBOARDED_MSG = (
    "u're connected just txt me what you want on ur gcal"
)

@router.get("/test")
async def test():
    return {"status": "ok"}

def _twiml(body: str | None, channel: str, phone: str) -> PlainTextResponse:
    """TwiML for the webhook. body=None returns an empty Response, sending nothing."""
    resp = _make_response(body, channel, phone) if body else MessagingResponse()
    return PlainTextResponse(str(resp), media_type="application/xml")


@router.post("/message", response_class=PlainTextResponse)
async def receive_message(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
):
    """
    Unified endpoint for both SMS and WhatsApp.
    Point both Twilio webhooks here.

    Anything that needs the model is answered out of band: Twilio gets an
    empty 200 straight away and the reply is delivered over the REST api when
    it's ready. Holding the webhook open meant the whole agent run had to fit
    in Twilio's ~15s timeout, which capped how much thinking it could do.
    Replies that need no model call still ride back on the response itself.
    """
    phone, channel = _parse_channel(From.strip())
    text = Body.strip()

    logger.info(f"[{channel}] {phone}: {text!r}")

    # Rate limit: 30 messages per user per hour (Redis-backed)
    if not await check_rate_limit(phone):
        return _twiml("one at a time bestie", channel, phone)

    async with get_db() as db:
        user, created = await get_or_create_user(db, phone)

        # New user, send onboarding link
        if not user.is_onboarded:
            auth_url = generate_auth_url(phone)
            #await send_vcard(phone)
            logger.info(f"Sent onboarding link to {phone} via {channel}")
            return _twiml(ONBOARDING_MSG.format(auth_url=auth_url), channel, phone)

        # Returning user texting connect or reconnect, re-auth
        if text.lower() in ("connect", "reconnect", "reauth", "reset"):
            auth_url = generate_auth_url(phone)
            return _twiml(f"no problem here's a fresh link: {auth_url}", channel, phone)

        # Read it out before the session closes, the background task opens its own
        refresh_token = user.google_refresh_token

    background_tasks.add_task(_reply_out_of_band, phone, text, channel, refresh_token)
    return _twiml(None, channel, phone)


async def _reply_out_of_band(phone: str, text: str, channel: str, refresh_token: str) -> None:
    """
    Runs after the webhook has already returned. Nothing here is on Twilio's
    clock, so a reply is always sent even if the agent takes its time.
    """
    reply = "sumn went wrong try again in a sec"

    try:
        async with get_db() as db:
            calendar_service = get_calendar_service_for_user(refresh_token)

            # If we asked "still want me to add it?", this text may be the answer.
            # Resolved here, before the model is consulted — the decision to write
            # to someone's calendar shouldn't hinge on the model's read of history.
            pending = await get_pending_event(db, phone)
            decision = classify_confirmation(text) if pending else "unrelated"

            if pending and decision == "confirm":
                await clear_pending_event(db, phone)
                reply = await perform_confirmed_action(pending, calendar_service)
                logger.info(f"Confirmed {pending.get('action', 'create')} for {phone}")

            elif pending and decision == "decline":
                await clear_pending_event(db, phone)
                reply = "bet, left it alone"

            else:
                # Anything that isn't a plain yes/no drops the parked action and
                # is handled as a fresh request. If it's still the same event,
                # the clash check simply runs again.
                if pending:
                    await clear_pending_event(db, phone)

                history = await get_recent_messages(db, phone)
                result = await run_agent(text, calendar_service, history=history)
                reply = result.text
                if result.pending_action:
                    await set_pending_event(db, phone, result.pending_action)

            # Only successful exchanges go in the transcript. Persisting a
            # "sumn went wrong" turn would poison context on the next text.
            await save_turn(db, phone, text, reply)

    except Exception as e:
        logger.exception(f"Agent error for {phone}: {e}")
        if "invalid_grant" in str(e).lower() or "token" in str(e).lower():
            reply = f"your google connection expired reconnect here: {generate_auth_url(phone)}"

    try:
        await send_message(phone, reply, channel=channel)
    except Exception as e:
        # Nothing left to fall back to — the user just gets silence
        logger.exception(f"Could not deliver reply to {phone}: {e}")


# Keep /sms as an alias so existing Twilio configs don't break
@router.post("/sms", response_class=PlainTextResponse)
async def receive_sms(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
):
    return await receive_message(
        request, background_tasks=background_tasks, From=From, Body=Body
    )
