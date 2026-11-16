from sqlalchemy import (
    Column, String, BigInteger, Integer, DateTime, Boolean, SmallInteger, ForeignKey,
)
from sqlalchemy.sql import func
from database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(String(36), primary_key=True, index=True)
    email_address = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255))
    avatar_url = Column(String(1000))
    color_hex = Column(String(7), default="#EF4444")
    encrypted_refresh_token = Column(String(1000), nullable=False)
    last_history_id = Column(BigInteger, nullable=False)
    watch_expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    last_synced_at = Column(DateTime, nullable=True)      # when the last sync finished
    sync_checkpoint = Column(DateTime, nullable=True)     # received time of the newest mail fully processed
    sync_error = Column(String(255), nullable=True)       # e.g. "Google sign-in expired"


class Email(Base):
    __tablename__ = "emails"

    id = Column(String(64), primary_key=True)
    thread_id = Column(String(64), nullable=False)
    account_id = Column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    sender_name = Column(String(255))
    sender_email = Column(String(255), nullable=False)
    subject = Column(String(1000))
    headline = Column(String(255), nullable=False)
    tldr_summary = Column(String(2000), nullable=False)
    signal_score = Column(SmallInteger, nullable=False)
    urgency_score = Column(SmallInteger, nullable=False)
    is_important = Column(Boolean, default=False)
    is_meeting = Column(Boolean, default=False)
    is_acknowledged = Column(Boolean, default=False)
    action_required = Column(String(255), nullable=True)
    meeting_time_iso = Column(DateTime, nullable=True)
    meeting_link = Column(String(1000), nullable=True)
    received_at = Column(DateTime, nullable=False)
    category = Column(String(100), index=True)
    user_marked_important = Column(Boolean, default=False)
    reminder_time_iso = Column(DateTime, nullable=True)

    deadline_at = Column(DateTime, nullable=True)         # date/time the user must act by or attend (UTC)
    cls_status = Column(String(12), nullable=False, default="done", server_default="done")  # done | provisional
    cls_attempts = Column(SmallInteger, nullable=False, default=0, server_default="0")
    older_loaded = Column(Boolean, nullable=False, default=False, server_default="0")  # temporary "Load older" row


class FeedbackEvent(Base):
    """One row per user action. Also acts as the tombstone that stops dismissed mails coming back."""
    __tablename__ = "feedback_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    account_id = Column(String(36), nullable=False, index=True)
    email_id = Column(String(64), nullable=False, index=True)
    action = Column(String(20), nullable=False)           # star | unstar | done | not_relevant | promo
    sender_key = Column(String(255))
    category = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())


class SenderWeight(Base):
    __tablename__ = "sender_weights"

    sender_key = Column(String(255), primary_key=True)
    weight = Column(SmallInteger, nullable=False, default=0)