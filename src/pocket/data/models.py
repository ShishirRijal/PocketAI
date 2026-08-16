"""ORM models. Mirrors §7 of the architecture doc.

Money is always integer minor units. Timestamps are UTC.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pocket.data.db import Base, UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(80))
    base_currency: Mapped[str] = mapped_column(String(3), default="EUR")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Tallinn")
    # which channel the digest / reminders go out on
    primary_channel: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    identities: Mapped[list[UserIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserIdentity(Base):
    """channel user id -> user. This is the allowlist.

    The doc had `users.channel_ids JSON`; a table is easier to index and to query
    the same way on sqlite and postgres.
    """

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("channel", "channel_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(16))
    channel_user_id: Mapped[str] = mapped_column(String(128))

    user: Mapped[User] = relationship(back_populates="identities")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    parent: Mapped[Category | None] = relationship(remote_side="Category.id")

    @property
    def full_name(self) -> str:
        return f"{self.parent.name}/{self.name}" if self.parent else self.name


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str | None] = mapped_column(String(16))  # merchant|activity|person|place


class TransactionTag(Base):
    __tablename__ = "transaction_tags"
    __table_args__ = (Index("ix_transaction_tags_tag_id", "tag_id"),)

    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_user_occurred", "user_id", "occurred_at"),
        Index("ix_transactions_user_cat_occurred", "user_id", "category_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    amount_base_minor: Mapped[int] = mapped_column(Integer)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    direction: Mapped[str] = mapped_column(String(10), default="expense")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    merchant: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    raw_message_id: Mapped[int | None] = mapped_column(ForeignKey("raw_messages.id"))
    llm_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    category: Mapped[Category | None] = relationship(lazy="joined")
    tags: Mapped[list[Tag]] = relationship(secondary="transaction_tags", lazy="selectin")


class TransactionVersion(Base):
    __tablename__ = "transaction_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    changed_by: Mapped[str] = mapped_column(String(10))  # user|llm|system
    diff: Mapped[dict[str, Any]] = mapped_column(JSON)  # {field: [old, new]}
    reason: Mapped[str | None] = mapped_column(Text)


class RawMessage(Base):
    __tablename__ = "raw_messages"
    __table_args__ = (UniqueConstraint("channel", "channel_msg_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(16))
    channel_msg_id: Mapped[str] = mapped_column(String(128))
    text: Mapped[str | None] = mapped_column(Text)
    media_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # processing bookkeeping, so a crashed worker can pick things back up
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    outcome: Mapped[str | None] = mapped_column(String(32))


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_message_id: Mapped[int | None] = mapped_column(ForeignKey("raw_messages.id"))
    purpose: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(80))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    request_json: Mapped[Any | None] = mapped_column(JSON)
    response_json: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )  # one open at a time
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
