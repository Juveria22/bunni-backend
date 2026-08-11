"""
User data access layer. All DB queries live here — nothing else touches the DB directly.
"""

import json
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from db.models import User, Message, PendingConfirmation, SentReminder

# How much conversation the agent gets to see. SMS threads are bursty —
# a text an hour later is almost always a new topic, and stale context
# makes the agent misresolve relative dates. 10 turns ≈ 5 exchanges.
HISTORY_LIMIT = 10
HISTORY_WINDOW_MINUTES = 60

# Guard rail against a pathological inbound message bloating the prompt
MAX_STORED_CHARS = 2000

# How long a "still want me to add it?" stays answerable. After this a "yes"
# is treated as an unrelated message rather than silently booking something
# the user has long forgotten about.
PENDING_CONFIRMATION_MINUTES = 30

# Only the last hour of conversation is ever read back. Keeping a week leaves
# room to debug a complaint, and holding people's message text indefinitely
# when nothing reads it is storage and privacy exposure for nothing.
MESSAGE_RETENTION_DAYS = 7

# Reminder rows only exist to stop a second send. Once the event is well past,
# nothing can re-trigger it, so they're dead weight.
REMINDER_RETENTION_DAYS = 2


async def get_user(db: AsyncSession, phone: str) -> User | None:
    result = await db.execute(select(User).where(User.phone_number == phone))
    return result.scalar_one_or_none()


async def get_or_create_user(db: AsyncSession, phone: str) -> tuple[User, bool]:
    """Returns (user, created). created=True if this is their first text."""
    user = await get_user(db, phone)
    if user:
        return user, False
    user = User(phone_number=phone)
    db.add(user)
    await db.flush()
    return user, True


async def save_google_tokens(
    db: AsyncSession,
    phone: str,
    refresh_token: str,
    email: str,
) -> User:
    # is_onboarded is "has a refresh token", so writing an empty one would
    # mark the account connected while leaving it unable to do anything
    if not refresh_token:
        raise ValueError("refusing to save an empty refresh token")

    user = await get_user(db, phone)
    if not user:
        user = User(phone_number=phone)
        db.add(user)
    user.google_refresh_token = refresh_token
    user.google_email = email
    user.onboarded_at = datetime.now(timezone.utc)
    await db.flush()
    return user


async def get_recent_messages(db: AsyncSession, phone: str) -> list[dict]:
    """
    Recent conversation turns for this user, oldest first, in the shape
    the Anthropic messages API expects: [{"role": ..., "content": ...}].

    Anthropic requires the list to start with a user turn, so any leading
    assistant messages (window cut mid-exchange) are dropped.
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
    """Append one exchange to the transcript. Timestamps keep them ordered."""
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
    """Park a clashing event until the user says yes or no. Replaces any prior one."""
    await clear_pending_event(db, phone)
    db.add(PendingConfirmation(
        phone_number=phone,
        event_json=json.dumps(args),
        created_at=datetime.now(timezone.utc),
    ))
    await db.flush()


async def get_pending_event(db: AsyncSession, phone: str) -> dict | None:
    """
    The parked event args, or None if there isn't one or it has gone stale.
    Stale and unreadable rows are cleared on the way out so they can't linger.
    """
    row = await db.get(PendingConfirmation, phone)
    if row is None:
        return None

    created = row.created_at
    # Postgres hands back an aware datetime; be defensive so a naive one
    # can't blow up the comparison and take the whole reply down
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    if created < datetime.now(timezone.utc) - timedelta(minutes=PENDING_CONFIRMATION_MINUTES):
        await clear_pending_event(db, phone)
        return None

    try:
        return json.loads(row.event_json)
    except ValueError:
        await clear_pending_event(db, phone)
        return None


async def clear_pending_event(db: AsyncSession, phone: str) -> None:
    await db.execute(
        delete(PendingConfirmation).where(PendingConfirmation.phone_number == phone)
    )
    await db.flush()


async def list_onboarded_users(db: AsyncSession) -> list[tuple[str, str]]:
    """(phone, refresh_token) for everyone the reminder sweep should look at."""
    result = await db.execute(
        select(User.phone_number, User.google_refresh_token)
        .where(User.google_refresh_token.is_not(None))
    )
    return [(row[0], row[1]) for row in result.all()]


async def claim_reminder(db: AsyncSession, phone: str, event_id: str, kind: str) -> bool:
    """
    Try to become the one who sends this reminder. True means claimed, send it;
    False means somebody already did.

    The insert is the lock — the primary key rejects a duplicate — so this is
    safe across workers without any coordination. It runs in a savepoint so a
    losing race doesn't poison the surrounding transaction.
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


async def prune_old_data(db: AsyncSession) -> tuple[int, int]:
    """
    Drop transcript rows past the retention window, and any parked confirmation
    that expired without being answered — those are only cleared when the user
    texts again, so someone who never replies leaves one behind forever.

    Returns (messages removed, pending removed).
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
    await db.flush()

    return messages.rowcount or 0, pending.rowcount or 0
