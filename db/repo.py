"""
User data access layer. All DB queries live here — nothing else touches the DB directly.
"""

from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from db.models import User


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
