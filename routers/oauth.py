"""
oauth callback router, google redirects here after the consent screen
redeem the single use state, store the tokens, send a welcome sms
"""

import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from db.session import get_db
from db.repo import save_google_tokens
from services.google_oauth import (
    resolve_state,
    exchange_code_for_tokens,
    MissingRefreshToken,
)
from services.sanitize import mask_phone
from services.sms import send_sms

logger = logging.getLogger(__name__)
router = APIRouter()

# the number is shown on purpose, state decides which phone controls whichever
# google account signed in, so a victim of a forwarded link needs to see a
# number that is not theirs before walking away
SUCCESS_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>connected</title>
  <style>
    body {{ font-family: system-ui, sans-serif; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; margin: 0; background: #f9f9f9; }}
    .card {{ text-align: center; padding: 2rem; max-width: 400px; }}
    h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
    p  {{ color: #666; font-size: 0.95rem; }}
    .num {{ font-family: ui-monospace, Menlo, monospace; color: #222; }}
    .warn {{ font-size: 0.85rem; color: #888; margin-top: 1.5rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>you're connected</h1>
    <p>your calendar is now linked to <span class="num">{masked_phone}</span></p>
    <p>head back to your texts, your calendar agent is ready to go</p>
    <p class="warn">not your number? that phone can now manage this calendar.
      remove access at <a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.</p>
  </div>
</body>
</html>
"""

ERROR_HTML = """
<!DOCTYPE html>
<html>
<head><title>error</title></head>
<body style="font-family:system-ui;text-align:center;padding:4rem">
  <h1>something went wrong</h1>
  <p>close this and text bunni again to get a new link.</p>
</body>
</html>
"""

# single use and expiring, so this is an ordinary outcome
# a refresh, a back button, or a link that sat unopened overnight
EXPIRED_HTML = """
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1"><title>link expired</title></head>
<body style="font-family:system-ui;text-align:center;padding:3rem;max-width:26rem;margin:0 auto">
  <h1 style="font-size:1.4rem">this link has expired</h1>
  <p style="color:#666">setup links work once and only for a few minutes.</p>
  <p style="color:#666">text bunni again for a fresh one.</p>
</body>
</html>
"""

# google only issues a refresh token on a genuinely new grant, an account with
# one outstanding can get an access token alone, useless when we need to act on
# the calendar hours later
NO_REFRESH_TOKEN_HTML = """
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1"><title>almost there</title></head>
<body style="font-family:system-ui;text-align:center;padding:3rem;max-width:26rem;margin:0 auto">
  <h1 style="font-size:1.4rem">almost there</h1>
  <p style="color:#666">google didn't give us permission to manage your calendar later on.</p>
  <p style="color:#666">remove bunni at
    <a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>,
    then text bunni for a fresh link.</p>
</body>
</html>
"""


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(request: Request, code: str = None, state: str = None, error: str = None):
    # user denied access, or google sent us nothing usable
    if error or not code or not state:
        logger.warning(f"OAuth callback error: {error}")
        return HTMLResponse(ERROR_HTML, status_code=400)

    # signature and timestamp, then redeem the nonce
    # redemption is a single DELETE ... RETURNING so the link works once
    async with get_db() as db:
        phone = await resolve_state(db, state)

    if not phone:
        return HTMLResponse(EXPIRED_HTML, status_code=400)

    try:
        refresh_token, email = await exchange_code_for_tokens(code)
    except MissingRefreshToken:
        # store nothing, so they are not left half connected
        # revoking is what makes google hand one over next attempt
        logger.warning("No refresh token returned during onboarding")
        return HTMLResponse(NO_REFRESH_TOKEN_HTML, status_code=400)
    except Exception as e:
        logger.exception(f"Token exchange failed: {e}")
        return HTMLResponse(ERROR_HTML, status_code=500)

    async with get_db() as db:
        await save_google_tokens(db, phone, refresh_token, email)

    # wrapped because the tokens are already saved, a twilio hiccup here should
    # not show an error page to someone fully connected who then re onboards
    try:
        await send_sms(
            to=phone,
            body="you're all set just text me anything like 'meeting with alex friday at 3pm' and i got it",
        )
    except Exception as e:
        logger.exception(f"Welcome sms failed after successful onboarding: {e}")

    logger.info(f"Onboarded: {mask_phone(phone)}")
    return HTMLResponse(SUCCESS_HTML.format(masked_phone=mask_phone(phone)))
