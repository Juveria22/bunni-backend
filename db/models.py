"""
db models
users, messages, reminder claims, parked confirmations, oauth nonces
users keyed by phone number, that is the identity
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

    # e.164, e.g. "+12015551234"
    phone_number = Column(String(20), primary_key=True, index=True)

    # google refresh token, encrypted app side when TOKEN_ENCRYPTION_KEY is set
    # see db/repo.py. rows written before the key are plaintext, so both shapes
    # must read back. a dump of this column is permanent calendar access
    google_refresh_token = Column(Text, nullable=True)
    google_email = Column(String(255), nullable=True)

    # "sms" | "whatsapp", last channel used
    # reminders are the biggest outbound volume, whatsapp is the cheaper path
    channel = Column(String(16), nullable=True)

    # lifecycle
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    onboarded_at = Column(DateTime(timezone=True), nullable=True)  # set when oauth completes

    @property
    def is_onboarded(self) -> bool:
        return self.google_refresh_token is not None


class Message(Base):
    """
    one row per conversation turn, user or assistant
    read back next text so the agent can resolve follow ups like "make it 3pm instead"
    """

    __tablename__ = "messages"

    # bigserial on postgres, plain integer on sqlite
    # sqlite only autoincrements integer pks, variant keeps a sqlite test db working
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

    # "user" or "assistant", matches the anthropic messages format
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # history is always read as "latest n for this phone", so index the pair
    __table_args__ = (
        Index("ix_messages_phone_created", "phone_number", "created_at"),
    )


class SentReminder(Base):
    """
    one row per reminder sent, the pk is the idempotency lock
    workers insert first and only text if the insert won, so no double sends
    row is released again if the send fails
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
    a write parked waiting on a yes/no, at most one per user, newest wins

    own table not columns on users on purpose: create_all makes missing tables
    but never adds columns to an existing one
    """

    __tablename__ = "pending_confirmations"

    phone_number = Column(
        String(20),
        ForeignKey("users.phone_number", ondelete="CASCADE"),
        primary_key=True,
    )

    # json of the tool args, replayed verbatim on confirm
    event_json = Column(Text, nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class OAuthState(Base):
    """
    single use nonce for one outstanding google auth link

    without it the state param is a permanent bearer token, anyone holding the
    url can bind their own number to whichever google account signs in
    consuming the row makes a link work once, the timestamp expires it
    """

    __tablename__ = "oauth_states"

    nonce = Column(String(64), primary_key=True)

    # not a foreign key on purpose, a cascade from users would kill an in flight
    # link if the account got cleared mid auth
    phone_number = Column(String(20), nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
