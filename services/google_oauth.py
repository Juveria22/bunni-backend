"""
Google OAuth2 service.
- Generates per-user auth URLs with state param (phone number, signed)
- Handles the callback and stores tokens per user
- Builds per-user calendar clients from stored refresh tokens
"""

import asyncio
import os
import hmac
import hashlib
import base64
import json
import logging
from datetime import datetime, timezone
from time import monotonic

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = "primary"

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]  # e.g. https://yourapp.railway.app/oauth/callback
STATE_SECRET = os.environ["STATE_SECRET"]  # any random 32-char string for HMAC signing


# ─────────────────────────────────────────────
# State token — encodes phone number securely
# so we know which user is coming back from Google
# ─────────────────────────────────────────────

def _sign(payload: str) -> str:
    sig = hmac.new(STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return sig


def encode_state(phone: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"phone": phone}).encode()).decode()
    sig = _sign(payload)
    return f"{payload}.{sig}"


def decode_state(state: str) -> str | None:
    """Returns phone number, or None if state is invalid/tampered."""
    try:
        payload, sig = state.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload).decode())
        return data["phone"]
    except Exception:
        return None


# ─────────────────────────────────────────────
# OAuth flow
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


def generate_auth_url(phone: str) -> str:
    """Generate a Google OAuth URL that encodes the user's phone in state."""
    flow = _make_flow()
    state = encode_state(phone)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",       # always return refresh token
        state=state,
        include_granted_scopes="true",
    )
    return auth_url


class MissingRefreshToken(Exception):
    """
    Google returned an access token but no refresh token.

    prompt="consent" is supposed to prevent this, but Google withholds one in
    some re-authorisation cases. Storing the null would leave the account
    looking un-onboarded forever, so the user gets told to retry instead.
    """


def exchange_code_for_tokens(code: str) -> tuple[str, str]:
    """
    Exchange OAuth code for tokens.
    Returns (refresh_token, email). Raises MissingRefreshToken if Google
    didn't give us one — without it we can't act on their calendar later.
    """
    flow = _make_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    if not creds.refresh_token:
        raise MissingRefreshToken("Google returned no refresh token")

    # Get user's email
    email = "unknown"
    try:
        import googleapiclient.discovery as gd
        service = gd.build("oauth2", "v2", credentials=creds)
        email = service.userinfo().get().execute().get("email", "unknown")
    except Exception as e:
        logger.warning(f"Could not fetch email (non-fatal): {e}")

    return creds.refresh_token, email


# ─────────────────────────────────────────────
# Per-user calendar client
# ─────────────────────────────────────────────

# What's cached is the access token, a plain string — NOT the built client.
# google-api-python-client sits on httplib2, which is not thread safe, and
# every call goes through asyncio.to_thread. Two messages from the same user
# arriving together would have shared one Http object across threads.
# Caching the token still skips the round trip to Google, which was the
# expensive part; assembling the client is local.
_TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60
_FALLBACK_TOKEN_TTL_SECONDS = 30 * 60
_MAX_CACHED_TOKENS = 1000
_token_cache: dict[str, tuple[float, str]] = {}


def _credentials(refresh_token: str, access_token: str | None = None) -> Credentials:
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def _fetch_access_token(refresh_token: str) -> tuple[str, float]:
    """Blocking: an http round trip to Google. Returns (token, expiry monotonic)."""
    creds = _credentials(refresh_token)
    creds.refresh(GoogleRequest())

    ttl = _FALLBACK_TOKEN_TTL_SECONDS
    if creds.expiry:
        # google-auth stores expiry as a naive utc datetime
        ttl = (creds.expiry - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()

    return creds.token, monotonic() + max(ttl - _TOKEN_REFRESH_MARGIN_SECONDS, 60)


def _build_service(refresh_token: str, access_token: str):
    """Blocking: reads the bundled discovery document. No network."""
    return build(
        "calendar", "v3",
        credentials=_credentials(refresh_token, access_token),
        cache_discovery=False,
    )


async def build_calendar_service(refresh_token: str):
    """
    A calendar client for this user. The access token is reused until it's
    close to expiring; the client itself is built fresh so nothing mutable is
    shared between threads.
    """
    cached = _token_cache.get(refresh_token)

    if cached and cached[0] > monotonic():
        access_token = cached[1]
    else:
        access_token, expires_at = await asyncio.to_thread(_fetch_access_token, refresh_token)
        if len(_token_cache) >= _MAX_CACHED_TOKENS:
            _token_cache.clear()  # blunt and bounded, they're cheap to refetch
        _token_cache[refresh_token] = (expires_at, access_token)

    return await asyncio.to_thread(_build_service, refresh_token, access_token)


def forget_calendar_service(refresh_token: str) -> None:
    """Drop a cached token, e.g. after Google rejects the refresh token."""
    _token_cache.pop(refresh_token, None)
