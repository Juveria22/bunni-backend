"""
google oauth2, turning a text message into a calendar we can touch

per user auth urls with a signed, single use, expiring state param
redeems the callback, revokes a grant on request
using the access once we have it lives in google_client.py
"""

import asyncio
import os
import hmac
import hashlib
import base64
import json
import logging
from time import time

import httpx
from googleapiclient.discovery import build as build_service
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession

from db.repo import create_oauth_state, consume_oauth_state, OAUTH_STATE_MINUTES
from services.google_client import (
    CLIENT_ID,
    CLIENT_SECRET,
    SCOPES,
    GOOGLE_HTTP_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]  # https://yourapp.railway.app/oauth/callback
STATE_SECRET = os.environ["STATE_SECRET"]  # any random 32 char string, hmac signing key

_STATE_MAX_AGE_SECONDS = OAUTH_STATE_MINUTES * 60


# ─────────────────────────────────────────────
# state token
#
# the only thing binding a consent screen to a phone number, so a signature
# alone is not enough. a state that never expires and replays is a permanent
# bearer token, forwarding your own link to a target is enough to bind their
# google account to your number
#
# three properties close it: an iat bounds the window, a server side nonce makes
# redemption single use, the signature stops both being edited
# ─────────────────────────────────────────────

def _sign(payload: str) -> str:
    return hmac.new(STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def encode_state(phone: str, nonce: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"phone": phone, "nonce": nonce, "iat": int(time())}).encode()
    ).decode()
    return f"{payload}.{_sign(payload)}"


def decode_state(state: str) -> tuple[str, str] | None:
    """
    (phone, nonce), or None if invalid, tampered with, or too old
    the nonce still has to be redeemed against the db to mean anything
    """
    try:
        payload, sig = state.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload).decode())

        issued_at = int(data["iat"])
        if abs(time() - issued_at) > _STATE_MAX_AGE_SECONDS:
            logger.warning("OAuth state rejected: outside the validity window")
            return None

        return data["phone"], data["nonce"]
    except Exception:
        return None


# ─────────────────────────────────────────────
# oauth flow
# ─────────────────────────────────────────────

def _make_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )


async def generate_auth_url(db: AsyncSession, phone: str) -> str:
    """auth url, one use, this phone number, next OAUTH_STATE_MINUTES"""
    nonce = await create_oauth_state(db, phone)
    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",       # ask for a refresh token every time
        state=encode_state(phone, nonce),
        include_granted_scopes="true",
    )
    return auth_url


async def resolve_state(db: AsyncSession, state: str) -> str | None:
    """
    the phone this callback belongs to, None if forged, expired, or used
    redemption is atomic so a link works once
    """
    decoded = decode_state(state)
    if decoded is None:
        return None

    phone, nonce = decoded
    claimed = await consume_oauth_state(db, nonce)

    if claimed is None:
        logger.warning("OAuth state rejected: nonce already used or expired")
        return None
    if claimed != phone:
        # signature and stored row disagree, should be impossible
        logger.error("OAuth state rejected: nonce/phone mismatch")
        return None

    return phone


class MissingRefreshToken(Exception):
    """
    google returned an access token but no refresh token

    prompt="consent" should prevent it, but google still withholds one on some
    re auths. storing the null leaves the account looking un onboarded forever,
    so the user is told to retry instead
    """


def _exchange_code_for_tokens(code: str) -> tuple[str, str]:
    """blocking, two http round trips. never call from the event loop directly"""
    flow = _make_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    if not creds.refresh_token:
        raise MissingRefreshToken("Google returned no refresh token")

    email = "unknown"
    try:
        service = build_service("oauth2", "v2", credentials=creds)
        email = service.userinfo().get().execute().get("email", "unknown")
    except Exception as e:
        logger.warning(f"Could not fetch email (non fatal): {e}")

    return creds.refresh_token, email


async def exchange_code_for_tokens(code: str) -> tuple[str, str]:
    """
    swap the oauth code for tokens, returns (refresh_token, email)

    threaded because both calls inside are blocking https round trips, on the
    event loop they stall every concurrent webhook for their combined duration
    """
    return await asyncio.to_thread(_exchange_code_for_tokens, code)


async def revoke_refresh_token(refresh_token: str) -> None:
    """
    tell google to forget the grant, best effort
    used on STOP, the local delete is what matters and a network failure here
    should not block it
    """
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS) as client:
            await client.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": refresh_token},
            )
    except Exception as e:
        logger.warning(f"Could not revoke google grant (non fatal): {e}")
