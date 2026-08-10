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


def exchange_code_for_tokens(code: str) -> tuple[str, str]:
    """
    Exchange OAuth code for tokens.
    Returns (refresh_token, email).
    """
    flow = _make_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

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

def get_calendar_service_for_user(refresh_token: str):
    """
    Build a Google Calendar service using a specific user's refresh token.

    Blocking: creds.refresh is an http round trip to Google. Call it through
    build_calendar_service rather than directly from a coroutine.
    """
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(GoogleRequest())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# Access tokens are good for an hour. Rebuilding the client on every message
# meant a round trip to Google's token endpoint before we could do anything,
# on every single text. Keyed by refresh token, which is the user identity here.
_SERVICE_TTL_SECONDS = 45 * 60
_MAX_CACHED_SERVICES = 500
_service_cache: dict[str, tuple[float, object]] = {}


async def build_calendar_service(refresh_token: str):
    """A calendar client for this user, reused until its access token nears expiry."""
    cached = _service_cache.get(refresh_token)
    if cached and cached[0] > monotonic():
        return cached[1]

    # Off the event loop — the token refresh inside is blocking http
    service = await asyncio.to_thread(get_calendar_service_for_user, refresh_token)

    if len(_service_cache) >= _MAX_CACHED_SERVICES:
        # Small, blunt, and bounded. These are cheap to rebuild.
        _service_cache.clear()
    _service_cache[refresh_token] = (monotonic() + _SERVICE_TTL_SECONDS, service)
    return service


def forget_calendar_service(refresh_token: str) -> None:
    """Drop a cached client, e.g. after Google rejects the refresh token."""
    _service_cache.pop(refresh_token, None)
