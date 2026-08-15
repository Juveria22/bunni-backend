"""
data access layer, every db query lives here
also owns refresh token encryption and the retention windows
"""

import json
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from db.models import User, Message, PendingConfirmation, SentReminder, OAuthState

logger = logging.getLogger(__name__)

# how much conversation the agent sees, 10 turns is ~5 exchanges
# sms threads are bursty, a text an hour later is a new topic and stale context
# makes the agent misresolve relative dates
HISTORY_LIMIT = 10
HISTORY_WINDOW_MINUTES = 60

# cap on one stored message
# history sits after the cache breakpoint so every char is full input price on
# every model call in the loop. nothing real over sms is this long
MAX_STORED_CHARS = 500

# how long a "still want me to add it?" stays answerable
# after this a "yes" is treated as unrelated, not a silent booking
PENDING_CONFIRMATION_MINUTES = 30

# how long an auth link stays redeemable
# long enough for the consent screen, short enough a forwarded link is dead
OAUTH_STATE_MINUTES = 15

# only the last hour is ever read back, a week is headroom for debugging
MESSAGE_RETENTION_DAYS = 7

# reminder rows only exist to stop a second send, dead weight once the event passed
REMINDER_RETENTION_DAYS = 2


# ---------- refresh token encryption ----------
# the column is a permanent credential, a db dump is unexpiring calendar access
# for every user. app layer encryption keeps the dump useless without the secret
#
# key is optional so an existing deploy does not break on the release that adds
# it. unprefixed rows read back as is, then get encrypted on the next write

_ENC_PREFIX = "enc:v1:"

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - declared in requirements
    Fernet = None
    InvalidToken = Exception

_TOKEN_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY")
_fernet = None

if _TOKEN_KEY and Fernet is not None:
    try:
        _fernet = Fernet(_TOKEN_KEY.encode())
    except Exception as e:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is set but unusable. It must be a urlsafe "
            "base64 32-byte key — generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"`"
        ) from e
else:
    # plain ascii, this lands on a windows console where an em dash is mojibake
    logger.warning(
        "TOKEN_ENCRYPTION_KEY not set, google refresh tokens are being stored "
        "in plaintext. Set it to encrypt them at rest."
    )


def encrypt_token(token: str) -> str:
    if _fernet is None:
        return token
    return _ENC_PREFIX + _fernet.encrypt(token.encode()).decode()


def decrypt_token(stored: str | None) -> str | None:
    """
    plaintext token, or None if unreadable

    None not raise on purpose, an unreadable token should read as "reconnect",
    not crash mid text
    """
    if not stored:
        return None
    if not stored.startswith(_ENC_PREFIX):
        return stored  # written before encryption was on
    if _fernet is None:
        logger.error("Stored token is encrypted but TOKEN_ENCRYPTION_KEY is not set")
        return None
    try:
        return _fernet.decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        logger.error("Stored token could not be decrypted - wrong TOKEN_ENCRYPTION_KEY?")
        return None


async def get_user(db: AsyncSession, phone: str) -> User | None:
    result = await db.execute(select(User).where(User.phone_number == phone))
    return result.scalar_one_or_none()


async def get_or_create_user(db: AsyncSession, phone: str, channel: str | None = None) -> tuple[User, bool]:
    """(user, created), created=True on their first text"""
    user = await get_user(db, phone)
    if user:
        # keep channel current, someone who moves to whatsapp gets reminders
        # on the cheaper one from then on
        if channel and user.channel != channel:
            user.channel = channel
            await db.flush()
        return user, False
    user = User(phone_number=phone, channel=channel)
    db.add(user)
    await db.flush()
    return user, True


async def save_google_tokens(
    db: AsyncSession,
    phone: str,
    refresh_token: str,
    email: str,
) -> User:
    # is_onboarded is "has a refresh token", an empty one marks the account
    # connected while it can do nothing
    if not refresh_token:
        raise ValueError("refusing to save an empty refresh token")

    user = await get_user(db, phone)
    if not user:
        user = User(phone_number=phone)
        db.add(user)
    user.google_refresh_token = encrypt_token(refresh_token)
    user.google_email = email
    user.onboarded_at = datetime.now(timezone.utc)
    await db.flush()
    return user


async def clear_google_token(db: AsyncSession, phone: str) -> None:
    """
    drop a refresh token google already rejected

    clearing only the in process cache left the row, so is_onboarded stayed true
    and the sweep retried a doomed refresh for that user every two minutes
    """
    user = await get_user(db, phone)
    if user and user.google_refresh_token is not None:
        user.google_refresh_token = None
        user.onboarded_at = None
        await db.flush()


async def delete_user_data(db: AsyncSession, phone: str) -> None:
    """
    everything held on one person, gone. used for STOP

    children cascade from users anyway, explicit deletes keep this correct if
    the user row is already missing or fks are not enforced
    """
    await db.execute(delete(Message).where(Message.phone_number == phone))
    await db.execute(delete(PendingConfirmation).where(PendingConfirmation.phone_number == phone))
    await db.execute(delete(SentReminder).where(SentReminder.phone_number == phone))
    await db.execute(delete(OAuthState).where(OAuthState.phone_number == phone))
    await db.execute(delete(User).where(User.phone_number == phone))
    await db.flush()


async def get_recent_messages(db: AsyncSession, phone: str) -> list[dict]:
    """
    recent turns, oldest first, in anthropic message shape

    the api needs the list to start on a user turn, so leading assistant rows
    from a window cut mid exchange get dropped
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=HISTORY_WINDOW_MINUTES)

    result = await db.execute(
        select(Message)
        .where(Message.phone_number == phone, Message.created_at >= cutoff)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(HISTORY_LIMIT)
    )
    rows = list(reversed(result.scalars().all()))

    while rows and rows[0].role != "user":
        rows.pop(0)

    return [{"role": m.role, "content": m.content} for m in rows]


async def save_turn(db: AsyncSession, phone: str, user_text: str, reply: str) -> None:
    """append one exchange, the 1ms offset keeps them ordered"""
    now = datetime.now(timezone.utc)
    db.add(Message(
        phone_number=phone,
        role="user",
        content=user_text[:MAX_STORED_CHARS],
        created_at=now,
    ))
    db.add(Message(
        phone_number=phone,
        role="assistant",
        content=reply[:MAX_STORED_CHARS],
        created_at=now + timedelta(milliseconds=1),
    ))
    await db.flush()


async def set_pending_event(db: AsyncSession, phone: str, args: dict) -> None:
    """park a write until the user says yes or no, replaces any prior one"""
    await clear_pending_event(db, phone)
    db.add(PendingConfirmation(
        phone_number=phone,
        event_json=json.dumps(args),
        created_at=datetime.now(timezone.utc),
    ))
    await db.flush()


async def claim_pending_event(db: AsyncSession, phone: str) -> dict | None:
    """
    take the parked action atomically, None if there is none or it went stale

    read then delete let two "yes" texts arriving together both execute, inbound
    messages run in their own task with no per user serialisation
    DELETE ... RETURNING means exactly one caller wins
    """
    result = await db.execute(
        delete(PendingConfirmation)
        .where(PendingConfirmation.phone_number == phone)
        .returning(PendingConfirmation.event_json, PendingConfirmation.created_at)
    )
    row = result.first()
    await db.flush()

    if row is None:
        return None

    event_json, created = row
    # postgres returns aware, be defensive so a naive one cannot blow up the
    # comparison and take the reply down
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    if created < datetime.now(timezone.utc) - timedelta(minutes=PENDING_CONFIRMATION_MINUTES):
        return None

    try:
        return json.loads(event_json)
    except ValueError:
        return None


async def clear_pending_event(db: AsyncSession, phone: str) -> None:
    await db.execute(
        delete(PendingConfirmation).where(PendingConfirmation.phone_number == phone)
    )
    await db.flush()


# ---------- one shot oauth links ----------

async def create_oauth_state(db: AsyncSession, phone: str) -> str:
    """
    issue a nonce for one auth link and remember who it was for

    drops any earlier link for this number, so at most one is live per person
    also stops the table growing a row per text from someone who never finishes
    """
    await db.execute(delete(OAuthState).where(OAuthState.phone_number == phone))
    nonce = secrets.token_urlsafe(24)
    db.add(OAuthState(
        nonce=nonce,
        phone_number=phone,
        created_at=datetime.now(timezone.utc),
    ))
    await db.flush()
    return nonce


async def consume_oauth_state(db: AsyncSession, nonce: str) -> str | None:
    """
    redeem a nonce once, returns the phone it was issued for
    None if it never existed, was used, or expired
    """
    result = await db.execute(
        delete(OAuthState)
        .where(OAuthState.nonce == nonce)
        .returning(OAuthState.phone_number, OAuthState.created_at)
    )
    row = result.first()
    await db.flush()

    if row is None:
        return None

    phone, created = row
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    if created < datetime.now(timezone.utc) - timedelta(minutes=OAUTH_STATE_MINUTES):
        return None

    return phone


async def list_onboarded_users(db: AsyncSession) -> list[tuple[str, str, str]]:
    """
    (phone, refresh_token, channel) for everyone the sweep should look at
    undecryptable tokens are skipped, they would fail every sweep forever
    """
    result = await db.execute(
        select(User.phone_number, User.google_refresh_token, User.channel)
        .where(User.google_refresh_token.is_not(None))
    )

    users = []
    for phone, stored, channel in result.all():
        token = decrypt_token(stored)
        if token:
            users.append((phone, token, channel or "sms"))
    return users


async def claim_reminder(db: AsyncSession, phone: str, event_id: str, kind: str) -> bool:
    """
    try to become the sender, True means claimed, False means someone else did

    the insert is the lock, the pk rejects duplicates, so no coordination needed
    runs in a savepoint so losing the race does not poison the transaction
    """
    try:
        async with db.begin_nested():
            db.add(SentReminder(
                phone_number=phone,
                event_id=event_id[:1024],
                kind=kind,
                sent_at=datetime.now(timezone.utc),
            ))
        return True
    except IntegrityError:
        return False


async def release_reminder(db: AsyncSession, phone: str, event_id: str, kind: str) -> None:
    """
    give a claim back when the send failed

    without it one twilio error marks the reminder delivered forever and the
    user never hears about it. the windows absorb a retry or two
    """
    await db.execute(
        delete(SentReminder).where(
            SentReminder.phone_number == phone,
            SentReminder.event_id == event_id[:1024],
            SentReminder.kind == kind,
        )
    )
    await db.flush()


async def prune_old_data(db: AsyncSession) -> tuple[int, int]:
    """
    drop expired transcript rows, unanswered confirmations, spent reminder
    claims and unfollowed auth links

    confirmations are otherwise only cleared when the user texts again, so
    someone who never replies leaves one behind forever

    returns (messages removed, pending removed)
    """
    now = datetime.now(timezone.utc)

    messages = await db.execute(
        delete(Message).where(
            Message.created_at < now - timedelta(days=MESSAGE_RETENTION_DAYS)
        )
    )
    pending = await db.execute(
        delete(PendingConfirmation).where(
            PendingConfirmation.created_at
            < now - timedelta(minutes=PENDING_CONFIRMATION_MINUTES)
        )
    )
    await db.execute(
        delete(SentReminder).where(
            SentReminder.sent_at < now - timedelta(days=REMINDER_RETENTION_DAYS)
        )
    )
    await db.execute(
        delete(OAuthState).where(
            OAuthState.created_at < now - timedelta(minutes=OAUTH_STATE_MINUTES)
        )
    )
    await db.flush()

    return messages.rowcount or 0, pending.rowcount or 0
