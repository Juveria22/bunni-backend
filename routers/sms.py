"""
inbound webhook router, sms and whatsapp, same logic for both
whatsapp is cheaper at scale, $0.02/conversation window vs $0.0079/sms segment

to enable whatsapp:
  1. apply for the twilio whatsapp business api, or use the sandbox
  2. set TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
  3. point the whatsapp webhook at /message, same endpoint as sms

channel comes off the From field, "whatsapp:+1..." vs "+1..."
"""

import asyncio
import os
import logging
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from db.session import get_db
from db.repo import (
    get_or_create_user,
    get_user,
    get_recent_messages,
    save_turn,
    claim_pending_event,
    set_pending_event,
    clear_google_token,
    delete_user_data,
    decrypt_token,
)
from services.google_client import build_calendar_service, forget_calendar_service
from services.google_oauth import generate_auth_url, revoke_refresh_token
from services.agent import (
    run_agent,
    classify_confirmation,
    interpret_confirmation,
    perform_confirmed_action,
)
from services.budget import over_budget
# aliased: this module already uses `count` for the rate limit's attempt tally
from services.monitoring import count as count_event, report
from services.rate_limit import check_rate_limit, claim_help_reply, RATE_LIMIT
from services.redis_client import try_acquire_lock
from services.sanitize import mask_phone
from services.sms import send_message

logger = logging.getLogger(__name__)
router = APIRouter()

WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")  # "whatsapp:+14155238886"

# hard ceiling on one inbound message
# the agent budgets itself but a hung google call is outside that budget, and
# silence is the worst outcome
TURN_DEADLINE_SECONDS = 60.0

# bounded concurrent replies
# each holds a google client, a thread pool slot (~32 total) and up to a minute
# of anthropic calls. the per phone rate limit does nothing about a thousand
# users texting at once, without this they queue until the deadline fires
MAX_CONCURRENT_REPLIES = 24
_reply_slots = asyncio.Semaphore(MAX_CONCURRENT_REPLIES)

# tracked so a deploy does not swallow mid flight work, twilio never retries
# these, the webhook already returned 200
_inflight: set[asyncio.Task] = set()

# public url, From is just a form value. without the signature check anyone can
# post From=<someone else's number> and read, move or delete their calendar
_validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
VALIDATE_SIGNATURE = os.environ.get("TWILIO_VALIDATE_SIGNATURE", "true").lower() != "false"

# the escape hatch is for local testing only, and it is silent otherwise, so a
# stray env var would hand anyone who finds the url every user's calendar
if not VALIDATE_SIGNATURE:
    logger.critical(
        "TWILIO_VALIDATE_SIGNATURE is off. Anyone who finds this url can read, "
        "move and delete any user's calendar by posting their number. Never "
        "run like this outside local testing"
    )

# wiping an account on a keyword needs the keyword to be unambiguous
# "cancel" is deliberately absent even though twilio opts out on it, it is a
# normal way to decline a clash question
_OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "quit"}
_HELP_KEYWORDS = {"help", "info"}

# twilio delivers at least once, and retries any webhook that doesn't return
# 200 inside ~15s. without this a redelivery is a second full agent run and a
# second billed segment for one text the user sent once. long enough to cover
# the retry schedule, short enough that the keys expire on their own
SEEN_MESSAGE_TTL_SECONDS = 900

# when the month's budget is gone we stop doing paid work, but going silent with
# no explanation is worse than one more segment. told once a day per person, so
# the notice itself cannot be what runs the bill up
BUDGET_NOTICE_TTL_SECONDS = 24 * 60 * 60
BUDGET_MSG = (
    "im maxed out for this month so i cant pick up new stuff right now, "
    "back at the start of next month"
)

HELP_MSG = (
    "text me plain english to manage ur google calendar, like "
    "'dentist friday at 3'. reply STOP to delete ur account. "
    "support: aminjuveria00@gmail.com"
)


async def _is_from_twilio(request: Request, form: dict) -> bool:
    """
    twilio signs the exact public url it posted to

    railway terminates tls and forwards http, so rebuild the url from the
    forwarded headers, the internal scheme would reject every real request
    """
    if not VALIDATE_SIGNATURE:
        return True

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    return _validator.validate(url, form, request.headers.get("X-Twilio-Signature", ""))


def _parse_channel(from_field: str) -> tuple[str, str]:
    """(phone, channel), twilio prefixes whatsapp senders with whatsapp:"""
    if from_field.startswith("whatsapp:"):
        return from_field.replace("whatsapp:", ""), "whatsapp"
    return from_field, "sms"


ONBOARDING_MSG = "heyy\n\nbunni setup: {auth_url}"


def _twiml(body: str | None, channel: str) -> PlainTextResponse:
    """
    twiml for the webhook response

    body=None sends nothing, used when the reply goes out over the rest api
    instead, and when staying silent is the point
    """
    resp = MessagingResponse()
    if body:
        msg = resp.message(body)
        if channel == "whatsapp" and WHATSAPP_NUMBER:
            msg.sender = WHATSAPP_NUMBER
    return PlainTextResponse(str(resp), media_type="application/xml")


def _spawn_reply(phone: str, text: str, channel: str, refresh_token: str) -> None:
    task = asyncio.create_task(_reply_out_of_band(phone, text, channel, refresh_token))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


def _spawn_revoke(refresh_token: str) -> None:
    """
    hand the google revoke to the background

    it is a third party call with a 15s timeout sitting on a webhook twilio
    abandons at 15s, and nothing downstream needs its answer
    """
    task = asyncio.create_task(revoke_refresh_token(refresh_token))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


async def drain_inflight(timeout: float = TURN_DEADLINE_SECONDS) -> None:
    """let in flight replies and revokes finish on shutdown instead of vanishing"""
    if _inflight:
        logger.info(f"Waiting on {len(_inflight)} in-flight replies")
        await asyncio.wait(set(_inflight), timeout=timeout)


async def _handle_opt_out(phone: str) -> None:
    """
    STOP means gone, revoke the grant at google, drop the token, delete every row

    no reply, twilio blocks outbound to this number after STOP anyway
    """
    async with get_db() as db:
        user = await get_user(db, phone)
        token = decrypt_token(user.google_refresh_token) if user else None

    if user is None:
        # already gone, twilio keeps delivering STOP for a while after the first
        return

    # delete first. revoking first put a 15s google call ahead of the delete on
    # a webhook twilio abandons at 15s, so a slow revoke killed the request with
    # the data still here, and the retry hit the same wall. STOP has to delete
    # whether or not google ever answers
    async with get_db() as db:
        await delete_user_data(db, phone)

    if token:
        forget_calendar_service(token)
        _spawn_revoke(token)

    logger.info(f"Opt out processed for {mask_phone(phone)}, all data deleted")


@router.post("/message", response_class=PlainTextResponse)
async def receive_message(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(None),
):
    """
    one endpoint for sms and whatsapp, point both twilio webhooks here

    anything needing the model is answered out of band, twilio gets an empty 200
    and the reply goes over the rest api when ready. holding the webhook open
    forced the whole agent run inside twilio's ~15s timeout
    replies that need no model call still ride back on the response
    """
    if not await _is_from_twilio(request, dict(await request.form())):
        # a run of these means either a misconfigured webhook or someone
        # probing, and both are worth seeing rather than counting quietly
        report("webhook.unsigned")
        raise HTTPException(status_code=403, detail="bad signature")

    phone, channel = _parse_channel(From.strip())
    text = Body.strip()
    keyword = text.lower().strip(".!? ")

    # after the signature check so an unsigned caller can't burn a real sid, and
    # ahead of everything else so a redelivery costs nothing. losing the lock
    # means someone already took this message, so stay silent, the first
    # delivery is answering it. redis down claims the lock and we process, a
    # duplicate reply beats dropping the only copy of someone's text
    if MessageSid and not await try_acquire_lock(
        f"msg:{MessageSid}", SEEN_MESSAGE_TTL_SECONDS
    ):
        logger.info(f"Ignored duplicate delivery of {MessageSid}")
        count_event("webhook.duplicate")
        return _twiml(None, channel)

    # length not content, logs have no retention window and sit outside STOP
    logger.info(f"[{channel}] {mask_phone(phone)}: {len(text)} chars")

    # ahead of the rate limit on purpose, opting out has to work on the message
    # someone sends after being told to slow down
    if keyword in _OPT_OUT_KEYWORDS:
        await _handle_opt_out(phone)
        return _twiml(None, channel)

    # also ahead of the rate limit so the keyword always works, but on its own
    # cooldown, answering every one made it an uncapped billable path
    if keyword in _HELP_KEYWORDS:
        if await claim_help_reply(phone):
            return _twiml(HELP_MSG, channel)
        return _twiml(None, channel)

    # 30 messages per user per hour, redis backed
    allowed, attempts = await check_rate_limit(phone)
    if not allowed:
        # answer the first one over the line then go quiet, replying every time
        # billed a segment per attempt for a client stuck in a loop
        count_event("message.rate_limited")
        if attempts == RATE_LIMIT + 1:
            return _twiml("one at a time bestie", channel)
        return _twiml(None, channel)

    # after STOP and HELP on purpose. leaving has to work when the money is
    # gone, and the help keyword is a carrier requirement, neither is optional
    if await over_budget():
        report("budget.exhausted", phone=mask_phone(phone))
        if await try_acquire_lock(f"budget-notice:{phone}", BUDGET_NOTICE_TTL_SECONDS):
            return _twiml(BUDGET_MSG, channel)
        return _twiml(None, channel)

    async with get_db() as db:
        user, _created = await get_or_create_user(db, phone, channel)

        # new user, send the onboarding link
        if not user.is_onboarded:
            auth_url = await generate_auth_url(db, phone)
            logger.info(f"Sent onboarding link to {mask_phone(phone)} via {channel}")
            return _twiml(ONBOARDING_MSG.format(auth_url=auth_url), channel)

        # returning user asking to re auth
        if keyword in ("connect", "reconnect", "reauth", "reset"):
            auth_url = await generate_auth_url(db, phone)
            return _twiml(f"no problem here's a fresh link: {auth_url}", channel)

        # read it out before the session closes, the background task opens its own
        refresh_token = decrypt_token(user.google_refresh_token)

    if not refresh_token:
        # encrypted under a key we cannot read right now, deliberately NOT cleared
        # a key missing because an env var has not propagated comes back, and
        # nulling every token on that deploy forces everyone to reconnect for
        # nothing. reconnecting overwrites the stored token anyway
        report("token.undecryptable", phone=mask_phone(phone))
        async with get_db() as db:
            auth_url = await generate_auth_url(db, phone)
        return _twiml(f"lost ur google connection, reconnect here: {auth_url}", channel)

    _spawn_reply(phone, text, channel, refresh_token)
    return _twiml(None, channel)


async def _reply_out_of_band(phone: str, text: str, channel: str, refresh_token: str) -> None:
    """
    runs after the webhook returned, nothing here is on twilio's clock

    wrapped in a hard deadline, the agent budgets itself but a hung google call
    is not covered by that and the failure mode is no answer at all
    """
    reply = "sumn went wrong try again in a sec"

    try:
        async with _reply_slots:
            reply = await asyncio.wait_for(
                _run_turn(phone, text, refresh_token), timeout=TURN_DEADLINE_SECONDS
            )
    except asyncio.TimeoutError:
        report("agent.timeout", phone=mask_phone(phone))
        # deliberately not "try again", the write may have landed and a blind
        # retry is how someone ends up with the event twice
        reply = "that took a while, check ur calendar before sending it again"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"Agent error for {mask_phone(phone)}: {e}")
        report("agent.error", e, phone=mask_phone(phone))
        # google's actual revoked/expired signal only, matching on "token" meant
        # any anthropic max_tokens error told the user to reconnect google
        if "invalid_grant" in str(e).lower():
            forget_calendar_service(refresh_token)
            async with get_db() as db:
                await clear_google_token(db, phone)
                auth_url = await generate_auth_url(db, phone)
            reply = f"your google connection expired reconnect here: {auth_url}"

    try:
        await send_message(phone, reply, channel=channel)
    except Exception as e:
        # nothing left to fall back to, the user gets silence, so this is the
        # one failure that is completely invisible from the outside
        logger.exception(f"Could not deliver reply to {mask_phone(phone)}: {e}")
        report("reply.delivery_failed", e, phone=mask_phone(phone))


async def _run_turn(phone: str, text: str, refresh_token: str) -> str:
    """
    work out the reply for one inbound message

    db sessions open around the queries and close before anything slow, holding
    one for the whole run tied up a pooled connection for as long as the agent
    thought, and thirty concurrent messages drained the pool
    """
    calendar_service = await build_calendar_service(refresh_token)

    # single DELETE ... RETURNING, two "yes" texts cannot both take the same
    # parked action, the loser gets None and falls through to the agent
    async with get_db() as db:
        pending = await claim_pending_event(db, phone)
        history = await get_recent_messages(db, phone)

    # this text may be the answer to a question we asked
    # resolved before the model runs, writing to a calendar should not hinge on
    # the model's read of history
    decision = "unrelated"
    if pending:
        # obvious yes/no is free, anything else goes to a small model rather
        # than making them repeat, what gets written is already fixed
        decision = classify_confirmation(text)
        if decision == "unrelated":
            decision = await interpret_confirmation(pending.get("question", ""), text)

    new_pending = None

    if pending and decision == "confirm":
        reply = await perform_confirmed_action(pending, calendar_service)
        logger.info(f"Confirmed {pending.get('action', 'create')} for {mask_phone(phone)}")

    elif pending and decision == "decline":
        reply = "bet, left it alone"

    else:
        # anything that is not a yes/no drops the parked action and runs as a
        # fresh request, the clash check just runs again if it is the same event
        result = await run_agent(text, calendar_service, history=history)
        reply = result.text
        new_pending = result.pending_action

    async with get_db() as db:
        if new_pending:
            # keep the question with it, the classifier needs what was asked to
            # read the answer
            await set_pending_event(db, phone, {**new_pending, "question": reply})
        # only successful exchanges go in the transcript, a "sumn went wrong"
        # turn would poison context on the next text
        await save_turn(db, phone, text, reply)

    return reply


# alias, keeps existing twilio configs working
@router.post("/sms", response_class=PlainTextResponse)
async def receive_sms(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(None),
):
    return await receive_message(
        request, From=From, Body=Body, MessageSid=MessageSid
    )
