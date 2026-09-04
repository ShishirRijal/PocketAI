"""Narrow query interfaces, one per aggregate. Callers pass in a Session so a
whole orchestrator step can run inside one transaction."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pocket.data.db import utcnow
from pocket.data.models import (
    Category,
    LLMCall,
    PendingAction,
    RawMessage,
    Tag,
    Transaction,
    TransactionTag,
    TransactionVersion,
    User,
    UserIdentity,
)

DEFAULT_CATEGORIES = [
    "Groceries",
    "Cafes",
    "Restaurants",
    "Transport",
    "Rent",
    "Utilities",
    "Subscriptions",
    "Shopping",
    "Health",
    "Entertainment",
    "Travel",
    "Education",
    "Gifts",
    "Salary",
    "Miscellaneous",
]


class UserRepo:
    def __init__(self, s: Session):
        self.s = s

    def get(self, user_id: int) -> User | None:
        return self.s.get(User, user_id)

    def by_identity(self, channel: str, channel_user_id: str) -> User | None:
        stmt = (
            select(User)
            .join(UserIdentity)
            .where(
                UserIdentity.channel == channel,
                UserIdentity.channel_user_id == channel_user_id,
            )
        )
        return self.s.scalars(stmt).first()

    def all(self) -> Sequence[User]:
        return self.s.scalars(select(User).order_by(User.id)).all()

    def create(
        self,
        *,
        name: str | None = None,
        base_currency: str = "EUR",
        timezone: str = "Europe/Tallinn",
        seed_categories: bool = True,
    ) -> User:
        user = User(name=name, base_currency=base_currency, timezone=timezone)
        self.s.add(user)
        self.s.flush()
        if seed_categories:
            for name_ in DEFAULT_CATEGORIES:
                self.s.add(Category(user_id=user.id, name=name_))
            self.s.flush()
        return user

    def add_identity(self, user: User, channel: str, channel_user_id: str) -> None:
        exists = self.s.scalars(
            select(UserIdentity).where(
                UserIdentity.channel == channel, UserIdentity.channel_user_id == channel_user_id
            )
        ).first()
        if exists:
            return
        self.s.add(UserIdentity(user_id=user.id, channel=channel, channel_user_id=channel_user_id))
        if user.primary_channel is None and channel != "cli":
            user.primary_channel = channel
        self.s.flush()

    def identity_for(self, user_id: int, channel: str) -> str | None:
        return self.s.scalars(
            select(UserIdentity.channel_user_id).where(
                UserIdentity.user_id == user_id, UserIdentity.channel == channel
            )
        ).first()


class CategoryRepo:
    def __init__(self, s: Session):
        self.s = s

    def active(self, user_id: int) -> Sequence[Category]:
        return self.s.scalars(
            select(Category)
            .where(Category.user_id == user_id, Category.archived_at.is_(None))
            .order_by(Category.name)
        ).all()

    def get(self, user_id: int, category_id: int) -> Category | None:
        c = self.s.get(Category, category_id)
        return c if c and c.user_id == user_id else None

    def by_name(self, user_id: int, name: str) -> Category | None:
        return self.s.scalars(
            select(Category).where(
                Category.user_id == user_id, func.lower(Category.name) == name.strip().lower()
            )
        ).first()

    def recently_used(self, user_id: int, limit: int = 20) -> list[Category]:
        """Most recently used first, then the rest alphabetically, capped at `limit`."""
        last_used = (
            select(Transaction.category_id, func.max(Transaction.created_at).label("last"))
            .where(Transaction.user_id == user_id, Transaction.deleted_at.is_(None))
            .group_by(Transaction.category_id)
            .subquery()
        )
        stmt = (
            select(Category)
            .outerjoin(last_used, last_used.c.category_id == Category.id)
            .where(Category.user_id == user_id, Category.archived_at.is_(None))
            .order_by(last_used.c.last.desc().nulls_last(), Category.name)
            .limit(limit)
        )
        return list(self.s.scalars(stmt).all())

    def create(self, user_id: int, name: str, parent_id: int | None = None) -> Category:
        existing = self.by_name(user_id, name)
        if existing:
            if existing.archived_at is not None:
                existing.archived_at = None
            return existing
        c = Category(user_id=user_id, name=name.strip(), parent_id=parent_id)
        self.s.add(c)
        self.s.flush()
        return c

    def rename(self, category: Category, new_name: str) -> None:
        category.name = new_name.strip()
        self.s.flush()

    def archive(self, category: Category) -> None:
        category.archived_at = utcnow()
        self.s.flush()

    def merchant_category(self, user_id: int, merchant: str) -> Category | None:
        """What category did this merchant land in most often? Learned mapping."""
        stmt = (
            select(Category, func.count(Transaction.id).label("n"))
            .join(Transaction, Transaction.category_id == Category.id)
            .where(
                Transaction.user_id == user_id,
                Transaction.deleted_at.is_(None),
                func.lower(Transaction.merchant) == merchant.strip().lower(),
                Category.archived_at.is_(None),
            )
            .group_by(Category.id)
            .order_by(func.count(Transaction.id).desc())
            .limit(1)
        )
        row = self.s.execute(stmt).first()
        return row[0] if row else None


class TagRepo:
    def __init__(self, s: Session):
        self.s = s

    def get_or_create(self, user_id: int, name: str, kind: str | None = None) -> Tag:
        name = normalize_tag(name)
        tag = self.s.scalars(select(Tag).where(Tag.user_id == user_id, Tag.name == name)).first()
        if tag:
            if kind and not tag.kind:
                tag.kind = kind
            return tag
        tag = Tag(user_id=user_id, name=name, kind=kind)
        self.s.add(tag)
        self.s.flush()
        return tag

    def all(self, user_id: int) -> Sequence[Tag]:
        return self.s.scalars(select(Tag).where(Tag.user_id == user_id).order_by(Tag.name)).all()

    def by_name(self, user_id: int, name: str) -> Tag | None:
        return self.s.scalars(
            select(Tag).where(Tag.user_id == user_id, Tag.name == normalize_tag(name))
        ).first()


def normalize_tag(name: str) -> str:
    name = name.strip().lstrip("#").lower()
    return "-".join(name.split())[:64]


# fields a user is allowed to change through edit; everything else is bookkeeping
EDITABLE_FIELDS = {
    "amount_minor",
    "currency",
    "amount_base_minor",
    "fx_rate",
    "direction",
    "category_id",
    "merchant",
    "note",
    "occurred_at",
}


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


class TransactionRepo:
    def __init__(self, s: Session):
        self.s = s

    def get(
        self, user_id: int, txn_id: int, *, include_deleted: bool = False
    ) -> Transaction | None:
        t = self.s.get(Transaction, txn_id)
        if not t or t.user_id != user_id:
            return None
        if t.deleted_at is not None and not include_deleted:
            return None
        return t

    def add(self, txn: Transaction, tags: Iterable[Tag] = ()) -> Transaction:
        self.s.add(txn)
        self.s.flush()
        for tag in {t.id: t for t in tags}.values():
            self.s.add(TransactionTag(transaction_id=txn.id, tag_id=tag.id))
        self.s.flush()
        self.s.refresh(txn)
        return txn

    def recent(self, user_id: int, limit: int = 5) -> list[Transaction]:
        stmt = (
            select(Transaction)
            .where(Transaction.user_id == user_id, Transaction.deleted_at.is_(None))
            .order_by(Transaction.created_at.desc(), Transaction.id.desc())
            .limit(limit)
        )
        return list(self.s.scalars(stmt).all())

    def update(
        self,
        txn: Transaction,
        changes: dict[str, Any],
        *,
        changed_by: str = "user",
        reason: str | None = None,
    ) -> dict[str, list[Any]]:
        """Apply changes and write a version row. Returns the diff actually applied."""
        diff: dict[str, list[Any]] = {}
        for field, new in changes.items():
            if field not in EDITABLE_FIELDS:
                raise ValueError(f"field {field!r} is not editable")
            old = getattr(txn, field)
            if old == new:
                continue
            diff[field] = [_jsonable(old), _jsonable(new)]
            setattr(txn, field, new)
        if diff:
            self.s.add(
                TransactionVersion(
                    transaction_id=txn.id, changed_by=changed_by, diff=diff, reason=reason
                )
            )
            self.s.flush()
        return diff

    def set_tags(self, txn: Transaction, tags: Iterable[Tag], *, reason: str | None = None) -> None:
        old = sorted(t.name for t in txn.tags)
        new_tags = {t.id: t for t in tags}
        new = sorted(t.name for t in new_tags.values())
        if old == new:
            return
        self.s.execute(
            TransactionTag.__table__.delete().where(TransactionTag.transaction_id == txn.id)
        )
        for tag in new_tags.values():
            self.s.add(TransactionTag(transaction_id=txn.id, tag_id=tag.id))
        self.s.add(
            TransactionVersion(
                transaction_id=txn.id, changed_by="user", diff={"tags": [old, new]}, reason=reason
            )
        )
        self.s.flush()
        self.s.expire(txn, ["tags"])

    def soft_delete(self, txn: Transaction, *, reason: str | None = None, by: str = "user") -> None:
        now = utcnow()
        self.s.add(
            TransactionVersion(
                transaction_id=txn.id,
                changed_by=by,
                diff={"deleted_at": [None, now.isoformat()]},
                reason=reason,
            )
        )
        txn.deleted_at = now
        self.s.flush()

    def restore(self, txn: Transaction, *, reason: str | None = None) -> None:
        if txn.deleted_at is None:
            return
        self.s.add(
            TransactionVersion(
                transaction_id=txn.id,
                changed_by="user",
                diff={"deleted_at": [txn.deleted_at.isoformat(), None]},
                reason=reason,
            )
        )
        txn.deleted_at = None
        self.s.flush()

    def versions(self, txn_id: int) -> Sequence[TransactionVersion]:
        return self.s.scalars(
            select(TransactionVersion)
            .where(TransactionVersion.transaction_id == txn_id)
            .order_by(TransactionVersion.id)
        ).all()

    def find_duplicates(
        self,
        user_id: int,
        *,
        amount_minor: int,
        currency: str,
        merchant: str | None,
        occurred_at: datetime,
        window: timedelta,
    ) -> list[Transaction]:
        """Same amount + currency (+ merchant if we have one), either logged recently
        or said to have happened within the window."""
        now = utcnow()
        conds = [
            Transaction.user_id == user_id,
            Transaction.deleted_at.is_(None),
            Transaction.amount_minor == amount_minor,
            Transaction.currency == currency,
            (Transaction.created_at >= now - window)
            | and_(
                Transaction.occurred_at >= occurred_at - window,
                Transaction.occurred_at <= occurred_at + window,
            ),
        ]
        if merchant:
            conds.append(func.lower(Transaction.merchant) == merchant.lower())
        return list(self.s.scalars(select(Transaction).where(*conds)).all())

    def in_range(
        self,
        user_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[Transaction]:
        conds = [Transaction.user_id == user_id]
        if not include_deleted:
            conds.append(Transaction.deleted_at.is_(None))
        if start:
            conds.append(Transaction.occurred_at >= start)
        if end:
            conds.append(Transaction.occurred_at < end)
        stmt = select(Transaction).where(*conds).order_by(Transaction.occurred_at)
        return list(self.s.scalars(stmt).all())


class RawMessageRepo:
    def __init__(self, s: Session):
        self.s = s

    def insert_idempotent(
        self,
        *,
        user_id: int,
        channel: str,
        channel_msg_id: str,
        text: str | None,
        media_json: list[dict[str, Any]] | None,
        received_at: datetime,
        user_channel_id: str | None = None,
        channel_meta: dict[str, Any] | None = None,
    ) -> tuple[RawMessage, bool]:
        """Returns (row, created). A retried webhook gets the existing row back."""
        existing = self.by_channel_id(channel, channel_msg_id)
        if existing:
            return existing, False
        row = RawMessage(
            user_id=user_id,
            channel=channel,
            channel_msg_id=channel_msg_id,
            text=text,
            media_json=media_json,
            received_at=received_at,
            user_channel_id=user_channel_id,
            channel_meta=channel_meta,
        )
        try:
            with self.s.begin_nested():
                self.s.add(row)
                self.s.flush()
        except IntegrityError:
            existing = self.by_channel_id(channel, channel_msg_id)
            assert existing is not None
            return existing, False
        return row, True

    def by_channel_id(self, channel: str, channel_msg_id: str) -> RawMessage | None:
        return self.s.scalars(
            select(RawMessage).where(
                RawMessage.channel == channel, RawMessage.channel_msg_id == channel_msg_id
            )
        ).first()

    def get(self, raw_id: int) -> RawMessage | None:
        return self.s.get(RawMessage, raw_id)

    def mark(self, raw: RawMessage, outcome: str) -> None:
        raw.processed_at = utcnow()
        raw.outcome = outcome
        self.s.flush()

    def unprocessed(
        self, older_than: timedelta = timedelta(0), limit: int = 50
    ) -> list[RawMessage]:
        cutoff = utcnow() - older_than
        stmt = (
            select(RawMessage)
            .where(
                (RawMessage.processed_at.is_(None)) | (RawMessage.outcome == "llm_unavailable"),
                RawMessage.received_at <= cutoff,
            )
            .order_by(RawMessage.id)
            .limit(limit)
        )
        return list(self.s.scalars(stmt).all())


class LLMCallRepo:
    def __init__(self, s: Session):
        self.s = s

    def log(self, **fields: Any) -> LLMCall:
        row = LLMCall(**fields)
        self.s.add(row)
        self.s.flush()
        return row

    def cost_since(self, since: datetime) -> float:
        total = self.s.scalar(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.created_at >= since)
        )
        return float(total or 0)

    def usage_by_day_model(self, since: datetime) -> list[dict[str, Any]]:
        day = func.date(LLMCall.created_at)
        stmt = (
            select(
                day.label("day"),
                LLMCall.model,
                func.count(LLMCall.id),
                func.sum(func.coalesce(LLMCall.cost_usd, 0)),
                func.sum(func.coalesce(LLMCall.prompt_tokens, 0)),
                func.sum(func.coalesce(LLMCall.completion_tokens, 0)),
                func.avg(LLMCall.latency_ms),
                func.sum(case((LLMCall.success.is_(True), 1), else_=0)),
            )
            .where(LLMCall.created_at >= since)
            .group_by(day, LLMCall.model)
            .order_by(day.desc(), LLMCall.model)
        )
        out = []
        for d, model, n, cost, pt, ct, lat, ok in self.s.execute(stmt).all():
            out.append(
                {
                    "day": str(d),
                    "model": model,
                    "calls": int(n),
                    "ok": int(ok or 0),
                    "cost_usd": float(cost or 0),
                    "prompt_tokens": int(pt or 0),
                    "completion_tokens": int(ct or 0),
                    "avg_latency_ms": round(float(lat or 0)),
                }
            )
        return out

    def purge_payloads(self, older_than: datetime) -> int:
        rows = self.s.scalars(
            select(LLMCall).where(
                LLMCall.created_at < older_than, LLMCall.request_json.is_not(None)
            )
        ).all()
        for r in rows:
            r.request_json = None
            r.response_json = None
        self.s.flush()
        return len(rows)


class PendingRepo:
    def __init__(self, s: Session):
        self.s = s

    def active(self, user_id: int) -> PendingAction | None:
        p = self.s.scalars(select(PendingAction).where(PendingAction.user_id == user_id)).first()
        if p and p.expires_at <= utcnow():
            self.s.delete(p)
            self.s.flush()
            return None
        return p

    def set(
        self, user_id: int, kind: str, payload: dict[str, Any], ttl: timedelta
    ) -> PendingAction:
        self.clear(user_id)
        p = PendingAction(user_id=user_id, kind=kind, payload=payload, expires_at=utcnow() + ttl)
        self.s.add(p)
        self.s.flush()
        return p

    def clear(self, user_id: int) -> None:
        for p in self.s.scalars(select(PendingAction).where(PendingAction.user_id == user_id)):
            self.s.delete(p)
        self.s.flush()
