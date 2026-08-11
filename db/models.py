"""
Database models. Two tables: users and messages.
Users are keyed by phone number — that's how we identify who's texting us.
Messages are the conversation transcript, so the agent has context on follow-ups.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, DateTime, Text, BigInteger, Integer, ForeignKey, Index,
)
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


class Message(Base):
    """
    One row per conversation turn, user or assistant.
    Read back on the next text so the agent can resolve follow-ups
    like "make it 3pm instead" or answers to a question it just asked.
    """

    __tablename__ = "messages"

    # BIGSERIAL on Postgres. sqlite only autoincrements plain INTEGER pks,
    # so use the variant there — lets the model run under a sqlite test db.
    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    phone_number = Column(
        String(20),
        ForeignKey("users.phone_number", ondelete="CASCADE"),
        nullable=False,
    )

    # "user" or "assistant" — matches the Anthropic messages format
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # History is always read as "latest N for this phone", so index that pair
    __table_args__ = (
        Index("ix_messages_phone_created", "phone_number", "created_at"),
    )


class SentReminder(Base):
    """
    One row per reminder actually sent. The primary key is what makes sending
    idempotent: every worker running the sweep tries to insert first and only
    texts if the insert won, so two workers can't both remind about the same
    event, and a restart mid-sweep can't send twice.
    """

    __tablename__ = "sent_reminders"

    phone_number = Column(
        String(20),
        ForeignKey("users.phone_number", ondelete="CASCADE"),
        primary_key=True,
    )
    event_id = Column(String(1024), primary_key=True)
    kind = Column(String(16), primary_key=True)  # "soon" | "now"

    sent_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class PendingConfirmation(Base):
    """
    An event creation that clashed with something already on the calendar and
    is parked waiting on a yes/no. At most one per user — a newer clash
    replaces the old one.

    This is its own table rather than columns on `users` on purpose: the app
    builds schema with metadata.create_all, which creates missing tables but
    will NOT add columns to a table that already exists. Columns on `users`
    would need a hand-written migration against the live database.
    """

    __tablename__ = "pending_confirmations"

    phone_number = Column(
        String(20),
        ForeignKey("users.phone_number", ondelete="CASCADE"),
        primary_key=True,
    )

    # JSON of the create_calendar_event args, replayed verbatim on confirm
    event_json = Column(Text, nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
