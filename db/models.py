"""
Database models. One table: users.
Primary key is phone number — that's how we identify who's texting us.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # E.164 phone number, e.g. "+12015551234"
    phone_number = Column(String(20), primary_key=True, index=True)

    # Google OAuth tokens (encrypted at rest via Supabase column encryption)
    google_refresh_token = Column(Text, nullable=True)
    google_email = Column(String(255), nullable=True)

    # Lifecycle
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    onboarded_at = Column(DateTime(timezone=True), nullable=True)  # set when OAuth completes

    @property
    def is_onboarded(self) -> bool:
        return self.google_refresh_token is not None
