"""
User data access layer. All DB queries live here — nothing else touches the DB directly.
"""

import json
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from db.models import User, Message, PendingConfirmation

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
