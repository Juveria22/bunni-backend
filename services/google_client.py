"""
builds an authenticated calendar client for one user

separate from google_oauth.py on purpose, that module is about granting access,
this one is about using access we already hold
knows nothing about redirect uris, consent screens or state
refresh token in, client out
"""

import asyncio
import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone
from time import monotonic

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build, build_from_document

logger = logging.getLogger(__name__)

# who we are to google and what we ask for
# the oauth flow imports these and adds its redirect uri on top
CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# every google call is bounded by this, well inside the 60s turn deadline
GOOGLE_HTTP_TIMEOUT_SECONDS = 15

# cached: the access token, a plain string. NOT the built client
# the client sits on httplib2, not thread safe, and every call goes through
# asyncio.to_thread, so two messages at once would share one Http across threads
# caching the token still skips the round trip, which was the expensive part
#
# the cap has to sit above the whole user base, not near it. the sweep walks
# every user in the same order every tick, which is the one access pattern that
# defeats both clear-when-full and lru: each entry is dropped just before its
# turn comes round again. at 1500 users against a 1000 cap either policy misses
# on nearly every user, every tick, and does a token round trip to google, which
# is the per-minute project quota rather than a bill
#
# so evict on expiry, which is free, and keep the count cap as a memory backstop
# only. entries are ~1kb, so 25k of them is ~25mb
_TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60
_FALLBACK_TOKEN_TTL_SECONDS = 30 * 60
_MAX_CACHED_TOKENS = 25_000

# compaction walks the whole cache, so it runs on crossing this mark rather than
# on every insert past the cap. without the slack a user base sitting above the
# ceiling rescans the cache once per user per tick, which costs more than the
# refreshes the cache exists to avoid
_TOKEN_CACHE_HIGH_WATER = _MAX_CACHED_TOKENS + _MAX_CACHED_TOKENS // 10

_token_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()


def _evict_token_cache() -> None:
    """Drop dead entries first, then oldest. Amortized, see the high water mark."""
    if len(_token_cache) < _TOKEN_CACHE_HIGH_WATER:
        return

    now = monotonic()
    for token in [t for t, (expires_at, _) in _token_cache.items() if expires_at <= now]:
        del _token_cache[token]

    # still over with everything live: the user base outgrew the ceiling, so
    # drop oldest and say so, from here token refreshes climb
    if len(_token_cache) > _MAX_CACHED_TOKENS:
        logger.warning(
            f"Token cache holds {len(_token_cache)} live entries, over the "
            f"{_MAX_CACHED_TOKENS} cap. Raise _MAX_CACHED_TOKENS, google token "
            "refreshes are about to climb"
        )
        while len(_token_cache) > _MAX_CACHED_TOKENS:
            _token_cache.popitem(last=False)

# the discovery doc is large, identical for everyone, and build() re parses it
# from disk on every call. the sweep does that once per user per tick, at 500
# users hundreds of thousands of parses a day on the thread pool
# parsed once here, the per call AuthorizedHttp stays per call, that is the
# part that is not thread safe
_discovery_doc: dict | None = None


def credentials(refresh_token: str, access_token: str | None = None) -> Credentials:
    """credentials for this user, also used by the oauth revoke path"""
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def _fetch_access_token(refresh_token: str) -> tuple[str, float]:
    """blocking, one http round trip. returns (token, expiry monotonic)"""
    creds = credentials(refresh_token)
    creds.refresh(GoogleRequest())

    ttl = _FALLBACK_TOKEN_TTL_SECONDS
    if creds.expiry:
        # google-auth stores expiry as naive utc
        ttl = (creds.expiry - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()

    return creds.token, monotonic() + max(ttl - _TOKEN_REFRESH_MARGIN_SECONDS, 60)


def _load_discovery_doc() -> dict | None:
    """bundled calendar v3 description, parsed once. None means fall back"""
    global _discovery_doc
    if _discovery_doc is None:
        try:
            from googleapiclient.discovery_cache import get_static_doc
            raw = get_static_doc("calendar", "v3")
            if raw:
                _discovery_doc = json.loads(raw)
        except Exception as e:
            # not fatal, build() just does it the slow way
            logger.warning(f"Could not preload calendar discovery doc: {e}")
    return _discovery_doc


def _build_service(refresh_token: str, access_token: str):
    """blocking, assembles a client from the cached description. no network"""
    # httplib2 has no default socket timeout, a stalled connection hangs the
    # worker thread forever. asyncio.wait_for frees the caller but cannot kill
    # the thread, so the bound has to be set here
    authed = AuthorizedHttp(
        credentials(refresh_token, access_token),
        http=httplib2.Http(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS),
    )

    doc = _load_discovery_doc()
    if doc is not None:
        return build_from_document(doc, http=authed)
    return build("calendar", "v3", http=authed, cache_discovery=False)


async def build_calendar_service(refresh_token: str):
    """
    calendar client for this user
    access token reused until close to expiry, client built fresh so nothing
    mutable is shared between threads
    """
    cached = _token_cache.get(refresh_token)

    if cached and cached[0] > monotonic():
        access_token = cached[1]
    else:
        access_token, expires_at = await asyncio.to_thread(_fetch_access_token, refresh_token)
        _token_cache[refresh_token] = (expires_at, access_token)
        _token_cache.move_to_end(refresh_token)
        _evict_token_cache()

    return await asyncio.to_thread(_build_service, refresh_token, access_token)


def forget_calendar_service(refresh_token: str) -> None:
    """drop a cached token, e.g. after google rejects the refresh token"""
    _token_cache.pop(refresh_token, None)
