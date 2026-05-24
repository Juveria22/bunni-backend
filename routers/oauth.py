"""
OAuth callback router.
Google redirects here after the user authorizes calendar access.
We decode the state (which contains their phone number),
store their tokens, and send them a welcome SMS.
"""

import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from db.session import get_db
from db.repo import save_google_tokens
from services.google_oauth import decode_state, exchange_code_for_tokens
from services.sms import send_sms

logger = logging.getLogger(__name__)
router = APIRouter()

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
  </style>
</head>
<body>
  <div class="card">
    <h1>you're connected</h1>
    <p>head back to your texts, your calendar agent is ready to go</p>
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
  <p>close this and text the agent again to get a new link.</p>
</body>
</html>
"""


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(request: Request, code: str = None, state: str = None, error: str = None):
    # User denied access
    if error or not code or not state:
        logger.warning(f"OAuth callback error: {error}")
        return HTMLResponse(ERROR_HTML, status_code=400)

    # Decode state → phone number (validates HMAC signature)
    phone = decode_state(state)
    if not phone:
        logger.warning("Invalid OAuth state parameter")
        return HTMLResponse(ERROR_HTML, status_code=400)

    try:
        refresh_token, email = exchange_code_for_tokens(code)
    except Exception as e:
        logger.exception(f"Token exchange failed for {phone}: {e}")
        return HTMLResponse(ERROR_HTML, status_code=500)

    # Store in DB
    async with get_db() as db:
        await save_google_tokens(db, phone, refresh_token, email)

    # Send welcome SMS
    await send_sms(
        to=phone,
        body="you're all set just text me anything like 'meeting with alex friday at 3pm' and i got it",
    )

    logger.info(f"Onboarded: {phone} ({email})")
    return HTMLResponse(SUCCESS_HTML)
