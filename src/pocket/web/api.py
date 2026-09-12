"""JSON API behind the dashboard (and a personal read/write API, §12.19).

All money in responses is integer minor units plus a currency code; the client
formats. Every endpoint takes the same filter params so charts, tiles and the
table always agree on the slice.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from pocket.core import money
from pocket.core.dates import local_midnight_utc, local_now, period_range
from pocket.data.models import (
    Category,
    LLMCall,
    RawMessage,
    Tag,
    Transaction,
    TransactionTag,
    User,
)
from pocket.data.repositories import CategoryRepo, TagRepo, TransactionRepo, normalize_tag
from pocket.web.auth import require_user

router = APIRouter(prefix="/api/v1", tags=["api"])


def rt(request: Request):
    return request.app.state.runtime


# ------------------------------------------------------------------ filters


@dataclass
class Filters:
    start: datetime
    end: datetime
    label: str
    categories: list[int]
    merchant: str | None
    tag: str | None
    q: str | None
    direction: str
    min_minor: int | None
    max_minor: int | None
    currency: str | None
    deleted: bool
    low_confidence: bool

    @property
    def days(self) -> int:
        return max(1, round((self.end - self.start).total_seconds() / 86400))


def _parse_filters(
    user: User,
    period: str | None,
    start: str | None,
    end: str | None,
    category: str | None,
    merchant: str | None,
    tag: str | None,
    q: str | None,
    direction: str,
    min_amount: float | None,
    max_amount: float | None,
    currency: str | None,
    deleted: bool,
    low_confidence: bool,
    s: Session,
) -> Filters:
    if start or end:
        today = local_now(user.timezone).date()
        a = date.fromisoformat(start) if start else today.replace(day=1)
        b = date.fromisoformat(end) if end else today
        if b < a:
            a, b = b, a
        rng_start = local_midnight_utc(a, user.timezone)
        rng_end = local_midnight_utc(b + timedelta(days=1), user.timezone)
        label = f"{a:%b %d, %Y} – {b:%b %d, %Y}"
    else:
        r = period_range(period or "this_month", user.timezone)
        rng_start, rng_end, label = r.start, r.end, r.label
    cat_ids: list[int] = []
    if category:
        wanted = {int(x) for x in category.split(",") if x.strip().isdigit()}
        cats = s.scalars(select(Category).where(Category.user_id == user.id)).all()
        # expand to children so "Food" includes "Food/Takeaway"
        changed = True
        while changed:
            changed = False
            for c in cats:
                if c.parent_id in wanted and c.id not in wanted:
                    wanted.add(c.id)
                    changed = True
        cat_ids = sorted(wanted)

    def to_minor(v: float | None) -> int | None:
        return money.to_minor(Decimal(str(v)), user.base_currency) if v is not None else None

    return Filters(
        start=rng_start,
        end=rng_end,
        label=label,
        categories=cat_ids,
        merchant=merchant or None,
        tag=normalize_tag(tag) if tag else None,
        q=(q or "").strip() or None,
        direction=direction,
        min_minor=to_minor(min_amount),
        max_minor=to_minor(max_amount),
        currency=currency.upper() if currency else None,
        deleted=deleted,
        low_confidence=low_confidence,
    )


def filters_dep(
    request: Request,
    user: User = Depends(require_user),
    period: str | None = Query(
        None, description="today, this_week, this_month, last_30_days, this_year, all_time, ..."
    ),
    start: str | None = Query(None, description="YYYY-MM-DD, overrides period"),
    end: str | None = Query(None, description="YYYY-MM-DD inclusive"),
    category: str | None = Query(None, description="comma separated category ids"),
    merchant: str | None = None,
    tag: str | None = None,
    q: str | None = Query(None, description="search merchant, note, category, tags"),
    direction: str = Query("expense", pattern="^(expense|income|transfer|all)$"),
    min_amount: float | None = None,
    max_amount: float | None = None,
    currency: str | None = None,
    deleted: bool = False,
    low_confidence: bool = False,
) -> tuple[User, Filters]:
    with rt(request).services.db.session() as s:
        f = _parse_filters(
            user, period, start, end, category, merchant, tag, q, direction,
            min_amount, max_amount, currency, deleted, low_confidence, s,
        )  # fmt: skip
    return user, f


def _conditions(
    user: User, f: Filters, *, with_range: bool = True, with_direction: bool = True
) -> list[Any]:
    conds: list[Any] = [Transaction.user_id == user.id]
    conds.append(
        Transaction.deleted_at.is_not(None) if f.deleted else Transaction.deleted_at.is_(None)
    )
    if with_range:
        conds += [Transaction.occurred_at >= f.start, Transaction.occurred_at < f.end]
    if with_direction and f.direction != "all":
        conds.append(Transaction.direction == f.direction)
    if f.categories:
        conds.append(Transaction.category_id.in_(f.categories))
    if f.merchant:
        conds.append(func.lower(Transaction.merchant) == f.merchant.lower())
    if f.min_minor is not None:
        conds.append(Transaction.amount_base_minor >= f.min_minor)
    if f.max_minor is not None:
        conds.append(Transaction.amount_base_minor <= f.max_minor)
    if f.currency:
        conds.append(Transaction.currency == f.currency)
    if f.low_confidence:
        conds.append(Transaction.llm_confidence < Decimal("0.85"))
    if f.tag:
        tagged = (
            select(TransactionTag.transaction_id)
            .join(Tag, Tag.id == TransactionTag.tag_id)
            .where(Tag.user_id == user.id, Tag.name == f.tag)
        )
        conds.append(Transaction.id.in_(tagged))
    if f.q:
        like = f"%{f.q.lower()}%"
        tag_hit = (
            select(TransactionTag.transaction_id)
            .join(Tag, Tag.id == TransactionTag.tag_id)
            .where(Tag.user_id == user.id, Tag.name.like(like))
        )
        cat_hit = select(Category.id).where(
            Category.user_id == user.id, func.lower(Category.name).like(like)
        )
        conds.append(
            or_(
                func.lower(Transaction.merchant).like(like),
                func.lower(Transaction.note).like(like),
                Transaction.category_id.in_(cat_hit),
                Transaction.id.in_(tag_hit),
            )
        )
    return conds


def txn_json(t: Transaction, raw_text: str | None = None) -> dict[str, Any]:
    return {
        "id": t.id,
        "amount_minor": t.amount_minor,
        "currency": t.currency,
        "amount_base_minor": t.amount_base_minor,
        "fx_rate": str(t.fx_rate) if t.fx_rate is not None else None,
        "direction": t.direction,
        "category_id": t.category_id,
        "category": t.category.full_name if t.category else None,
        "merchant": t.merchant,
        "note": t.note,
        "tags": [{"name": x.name, "kind": x.kind} for x in t.tags],
        "occurred_at": t.occurred_at.isoformat(),
        "created_at": t.created_at.isoformat(),
        "confidence": float(t.llm_confidence) if t.llm_confidence is not None else None,
        "deleted": t.deleted_at is not None,
        "raw_message_id": t.raw_message_id,
        "raw_text": raw_text,
    }


# ------------------------------------------------------------------ endpoints


@router.get("/me")
async def me(request: Request, user: User = Depends(require_user)) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "base_currency": user.base_currency,
        "timezone": user.timezone,
        "symbol": money.SYMBOLS.get(user.base_currency, user.base_currency + " ").strip(),
    }


@router.get("/transactions")
async def list_transactions(
    request: Request,
    uf: tuple[User, Filters] = Depends(filters_dep),
    sort: str = Query("occurred_at", pattern="^(occurred_at|amount|merchant|category|created_at)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    user, f = uf
    conds = _conditions(user, f)
    sort_col = {
        "occurred_at": Transaction.occurred_at,
        "created_at": Transaction.created_at,
        "amount": Transaction.amount_base_minor,
        "merchant": func.lower(func.coalesce(Transaction.merchant, "")),
        "category": func.lower(func.coalesce(Category.name, "")),
    }[sort]
    with rt(request).services.db.session() as s:
        total, total_minor = s.execute(
            select(
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.amount_base_minor), 0),
            ).where(*conds)
        ).one()
        stmt = (
            select(Transaction)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(*conds)
        )
        stmt = stmt.order_by(
            sort_col.desc() if order == "desc" else sort_col.asc(), Transaction.id.desc()
        )
        rows = s.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).unique().all()
        items = [txn_json(t) for t in rows]
    return {
        "items": items,
        "total": total,
        "total_minor": int(total_minor),
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
        "currency": user.base_currency,
        "range": {"start": f.start.isoformat(), "end": f.end.isoformat(), "label": f.label},
    }


@router.get("/summary")
async def summary(
    request: Request, uf: tuple[User, Filters] = Depends(filters_dep)
) -> dict[str, Any]:
    """Everything the overview needs in one round trip."""
    user, f = uf
    with rt(request).services.db.session() as s:
        return _summary(s, user, f)


def _summary(s: Session, user: User, f: Filters) -> dict[str, Any]:
    tz = ZoneInfo(user.timezone)
    # this period, both directions, so income/net tiles work under any direction filter
    rows = (
        s.scalars(select(Transaction).where(*_conditions(user, f, with_direction=False)))
        .unique()
        .all()
    )
    span = f.end - f.start
    prev = Filters(**{**f.__dict__, "start": f.start - span, "end": f.start})
    prev_rows = s.execute(
        select(
            Transaction.direction,
            func.sum(Transaction.amount_base_minor),
            func.count(Transaction.id),
        )
        .where(*_conditions(user, prev, with_direction=False))
        .group_by(Transaction.direction)
    ).all()
    # 12 months of history ending with the current range, for the trend chart
    end_local = (f.end - timedelta(seconds=1)).astimezone(tz).date()
    first = date(end_local.year - (1 if end_local.month < 12 else 0), (end_local.month % 12) + 1, 1)
    hist = Filters(
        **{**f.__dict__, "start": local_midnight_utc(first, user.timezone), "end": f.end}
    )
    hist_rows = s.execute(
        select(Transaction.occurred_at, Transaction.direction, Transaction.amount_base_minor).where(
            *_conditions(user, hist, with_direction=False)
        )
    ).all()

    want = (lambda d: True) if f.direction == "all" else (lambda d: d == f.direction)
    spend_rows = [r for r in rows if want(r.direction)]
    totals = {"expense": 0, "income": 0, "transfer": 0}
    counts: Counter[str] = Counter()
    for r in rows:
        totals[r.direction] = totals.get(r.direction, 0) + r.amount_base_minor
        counts[r.direction] += 1
    prev_tot = {d: int(v or 0) for d, v, _ in prev_rows}
    prev_cnt = {d: int(n) for d, _, n in prev_rows}
    main_total = sum(r.amount_base_minor for r in spend_rows)

    # time series: daily for <= 62 days, else weekly
    daily = f.days <= 62
    buckets: dict[str, int] = {}
    cur_day = f.start.astimezone(tz).date()
    last_day = (f.end - timedelta(seconds=1)).astimezone(tz).date()
    today = local_now(user.timezone).date()
    if f.days > 3660:  # all_time: start from the first transaction
        firsts = [r.occurred_at.astimezone(tz).date() for r in spend_rows]
        cur_day = min(firsts) if firsts else today
    last_day = min(last_day, today)
    step = timedelta(days=1 if daily else 7)
    if not daily:
        cur_day -= timedelta(days=cur_day.weekday())
    while cur_day <= last_day:
        buckets[cur_day.isoformat()] = 0
        cur_day += step
    for r in spend_rows:
        d = r.occurred_at.astimezone(tz).date()
        if not daily:
            d -= timedelta(days=d.weekday())
        k = d.isoformat()
        if k in buckets:
            buckets[k] += r.amount_base_minor

    by_cat: dict[tuple[int | None, str], list[int]] = defaultdict(lambda: [0, 0])
    by_merchant: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_tag: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    weekday = [0] * 7
    for r in spend_rows:
        key = (r.category_id, r.category.full_name if r.category else "Uncategorized")
        by_cat[key][0] += r.amount_base_minor
        by_cat[key][1] += 1
        if r.merchant:
            by_merchant[r.merchant][0] += r.amount_base_minor
            by_merchant[r.merchant][1] += 1
        for t in r.tags:
            by_tag[t.name][0] += r.amount_base_minor
            by_tag[t.name][1] += 1
        weekday[r.occurred_at.astimezone(tz).weekday()] += r.amount_base_minor

    months: dict[str, dict[str, int]] = {}
    m = first
    while m <= end_local:
        months[m.strftime("%Y-%m")] = {"expense": 0, "income": 0}
        m = (m + timedelta(days=32)).replace(day=1)
    for occurred, direction, amt in hist_rows:
        k = occurred.astimezone(tz).strftime("%Y-%m")
        if k in months and direction in ("expense", "income"):
            months[k][direction] += amt

    elapsed_days = max(1, min(f.days, (min(f.end, datetime.now(tz)) - f.start).days + 1))
    largest = sorted(spend_rows, key=lambda r: r.amount_base_minor, reverse=True)[:5]

    def ranked(d: dict, limit: int) -> list[dict[str, Any]]:
        items = sorted(d.items(), key=lambda kv: kv[1][0], reverse=True)
        return [{"name": k, "total_minor": v[0], "count": v[1]} for k, v in items[:limit]]

    return {
        "currency": user.base_currency,
        "range": {
            "start": f.start.isoformat(),
            "end": f.end.isoformat(),
            "label": f.label,
            "days": f.days,
        },
        "direction": f.direction,
        "total_minor": main_total,
        "count": len(spend_rows),
        "avg_per_day_minor": round(main_total / elapsed_days),
        "avg_per_txn_minor": round(main_total / len(spend_rows)) if spend_rows else 0,
        "expense_minor": totals["expense"],
        "income_minor": totals["income"],
        "net_minor": totals["income"] - totals["expense"],
        "counts": dict(counts),
        "previous": {
            "expense_minor": prev_tot.get("expense", 0),
            "income_minor": prev_tot.get("income", 0),
            "count": sum(prev_cnt.values()),
            "total_minor": sum(v for d, v in prev_tot.items() if want(d)),
        },
        "series": {
            "granularity": "day" if daily else "week",
            "points": [{"date": k, "total_minor": v} for k, v in buckets.items()],
        },
        "by_category": [
            {"id": cid, "name": name, "total_minor": v[0], "count": v[1]}
            for (cid, name), v in sorted(by_cat.items(), key=lambda kv: kv[1][0], reverse=True)
        ],
        "by_merchant": ranked(by_merchant, 10),
        "by_tag": ranked(by_tag, 12),
        "by_weekday": [
            {"day": d, "total_minor": weekday[i]}
            for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        ],
        "months": [{"month": k, **v} for k, v in months.items()],
        "largest": [txn_json(t) for t in largest],
    }


@router.get("/facets")
async def facets(request: Request, user: User = Depends(require_user)) -> dict[str, Any]:
    """Options for the filter dropdowns."""
    with rt(request).services.db.session() as s:
        cats = CategoryRepo(s).active(user.id)
        merchants = s.execute(
            select(Transaction.merchant, func.count(Transaction.id))
            .where(
                Transaction.user_id == user.id,
                Transaction.deleted_at.is_(None),
                Transaction.merchant.is_not(None),
            )
            .group_by(Transaction.merchant)
            .order_by(func.count(Transaction.id).desc())
            .limit(200)
        ).all()
        tags = TagRepo(s).all(user.id)
        currencies = s.scalars(
            select(Transaction.currency)
            .where(Transaction.user_id == user.id)
            .group_by(Transaction.currency)
        ).all()
        return {
            "categories": [
                {"id": c.id, "name": c.full_name, "parent_id": c.parent_id} for c in cats
            ],
            "merchants": [{"name": m, "count": n} for m, n in merchants],
            "tags": [{"name": t.name, "kind": t.kind} for t in tags],
            "currencies": sorted(currencies),
        }


@router.get("/transactions/{txn_id}")
async def get_transaction(
    request: Request, txn_id: int, user: User = Depends(require_user)
) -> dict[str, Any]:
    with rt(request).services.db.session() as s:
        t = TransactionRepo(s).get(user.id, txn_id, include_deleted=True)
        if not t:
            raise HTTPException(404, "not found")
        raw = s.get(RawMessage, t.raw_message_id) if t.raw_message_id else None
        versions = TransactionRepo(s).versions(t.id)
        calls = (
            s.scalars(
                select(LLMCall)
                .where(LLMCall.raw_message_id == t.raw_message_id)
                .order_by(LLMCall.id)
            ).all()
            if t.raw_message_id
            else []
        )
        return {
            **txn_json(t, raw.text if raw else None),
            "channel": raw.channel if raw else None,
            "versions": [
                {
                    "at": v.changed_at.isoformat(),
                    "by": v.changed_by,
                    "diff": v.diff,
                    "reason": v.reason,
                }
                for v in versions
            ],
            "llm_calls": [
                {
                    "purpose": c.purpose,
                    "model": c.model,
                    "success": c.success,
                    "latency_ms": c.latency_ms,
                    "cost_usd": float(c.cost_usd or 0),
                }
                for c in calls
            ],
        }


class TxnPatch(BaseModel):
    amount: float | None = None
    currency: str | None = None
    category_id: int | None = None
    merchant: str | None = None
    note: str | None = None
    occurred_at: str | None = None  # ISO date or datetime (local)
    direction: str | None = None
    tags: list[str] | None = None


@router.patch("/transactions/{txn_id}")
async def patch_transaction(
    request: Request, txn_id: int, body: TxnPatch, user: User = Depends(require_user)
) -> dict[str, Any]:
    from pocket.core.dates import resolve_occurred_at

    fx = rt(request).services.fx
    with rt(request).services.db.session() as s:
        repo = TransactionRepo(s)
        t = repo.get(user.id, txn_id)
        if not t:
            raise HTTPException(404, "not found")
        changes: dict[str, Any] = {}
        fields = body.model_fields_set
        if "category_id" in fields:
            if body.category_id is not None and not CategoryRepo(s).get(user.id, body.category_id):
                raise HTTPException(400, "unknown category")
            changes["category_id"] = body.category_id
        for fld in ("merchant", "note"):
            if fld in fields:
                changes[fld] = (getattr(body, fld) or "").strip() or None
        if "direction" in fields:
            if body.direction not in ("expense", "income", "transfer"):
                raise HTTPException(400, "bad direction")
            changes["direction"] = body.direction
        if "occurred_at" in fields and body.occurred_at:
            changes["occurred_at"] = resolve_occurred_at(body.occurred_at, user.timezone)
        cur = (body.currency or t.currency).upper()
        if body.amount is not None or cur != t.currency:
            amt = (
                money.to_minor(Decimal(str(body.amount)), cur)
                if body.amount is not None
                else t.amount_minor
            )
            if cur == user.base_currency:
                base, rate = amt, None
            else:
                r = await fx.rate(cur, user.base_currency)
                base, rate = money.convert_minor(amt, cur, user.base_currency, r.rate), r.rate
            changes.update(amount_minor=amt, currency=cur, amount_base_minor=base, fx_rate=rate)
        repo.update(t, changes, changed_by="user", reason="dashboard edit")
        if body.tags is not None:
            repo.set_tags(
                t,
                [TagRepo(s).get_or_create(user.id, n) for n in body.tags if n.strip()],
                reason="dashboard edit",
            )
        s.flush()
        s.refresh(t)
        return txn_json(t)


@router.delete("/transactions/{txn_id}")
async def delete_transaction(
    request: Request, txn_id: int, user: User = Depends(require_user)
) -> dict[str, Any]:
    with rt(request).services.db.session() as s:
        t = TransactionRepo(s).get(user.id, txn_id)
        if not t:
            raise HTTPException(404, "not found")
        TransactionRepo(s).soft_delete(t, reason="dashboard delete")
        return {"deleted": True}


@router.post("/transactions/{txn_id}/restore")
async def restore_transaction(
    request: Request, txn_id: int, user: User = Depends(require_user)
) -> dict[str, Any]:
    with rt(request).services.db.session() as s:
        t = TransactionRepo(s).get(user.id, txn_id, include_deleted=True)
        if not t:
            raise HTTPException(404, "not found")
        TransactionRepo(s).restore(t, reason="dashboard restore")
        return {"restored": True}


class SayIn(BaseModel):
    text: str


@router.post("/say")
async def say(request: Request, body: SayIn, user: User = Depends(require_user)) -> dict[str, Any]:
    """Quick-log from the dashboard: same pipeline as any chat channel."""
    import uuid

    from pocket.channels.base import InboundMessage
    from pocket.data.repositories import UserRepo

    runtime = rt(request)
    text = body.text.strip()[:500]
    if not text:
        raise HTTPException(400, "empty")
    ucid = f"user:{user.id}"
    with runtime.services.db.session() as s:
        repo = UserRepo(s)
        u = repo.get(user.id)
        assert u is not None
        repo.add_identity(u, "web", ucid)
    msg = InboundMessage(
        channel="web", channel_msg_id=uuid.uuid4().hex, user_channel_id=ucid, text=text
    )
    res = await runtime.ingestor.ingest(msg, enqueue=False)
    if res.status.value != "queued" or res.raw_message_id is None:
        raise HTTPException(429 if res.status.value == "rate_limited" else 400, res.status.value)
    replies = await runtime.services.orchestrator.handle_raw(res.raw_message_id)
    return {"replies": [r.model_dump() for r in replies]}


class CategoryIn(BaseModel):
    name: str
    parent_id: int | None = None


@router.post("/categories")
async def create_category(
    request: Request, body: CategoryIn, user: User = Depends(require_user)
) -> dict[str, Any]:
    with rt(request).services.db.session() as s:
        c = CategoryRepo(s).create(user.id, body.name, parent_id=body.parent_id)
        return {"id": c.id, "name": c.full_name}


@router.get("/export.csv")
async def export_csv(
    request: Request, uf: tuple[User, Filters] = Depends(filters_dep)
) -> StreamingResponse:
    user, f = uf
    with rt(request).services.db.session() as s:
        rows = (
            s.scalars(
                select(Transaction).where(*_conditions(user, f)).order_by(Transaction.occurred_at)
            )
            .unique()
            .all()
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "id",
                "date",
                "amount",
                "currency",
                f"amount_{user.base_currency.lower()}",
                "direction",
                "category",
                "merchant",
                "tags",
                "note",
            ]
        )
        tz = ZoneInfo(user.timezone)
        for t in rows:
            w.writerow([
                t.id, t.occurred_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                money.from_minor(t.amount_minor, t.currency), t.currency,
                money.from_minor(t.amount_base_minor, user.base_currency), t.direction,
                t.category.full_name if t.category else "", t.merchant or "",
                " ".join(x.name for x in t.tags), t.note or "",
            ])  # fmt: skip
    buf.seek(0)
    name = f"pocket-{f.start.astimezone(tz):%Y%m%d}-{(f.end - timedelta(seconds=1)).astimezone(tz):%Y%m%d}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/system")
async def system(request: Request, user: User = Depends(require_user)) -> dict[str, Any]:
    """Ops view: LLM spend, model health, pipeline outcomes."""
    runtime = rt(request)
    since = datetime.now(ZoneInfo("UTC")) - timedelta(days=30)
    with runtime.services.db.session() as s:
        from pocket.data.repositories import LLMCallRepo

        usage = LLMCallRepo(s).usage_by_day_model(since)
        outcomes = dict(
            s.execute(
                select(RawMessage.outcome, func.count(RawMessage.id))
                .where(RawMessage.user_id == user.id, RawMessage.received_at >= since)
                .group_by(RawMessage.outcome)
            ).all()
        )
        recent = s.scalars(
            select(RawMessage)
            .where(RawMessage.user_id == user.id)
            .order_by(RawMessage.id.desc())
            .limit(25)
        ).all()
        recent_json = [
            {
                "id": r.id,
                "channel": r.channel,
                "text": r.text,
                "outcome": r.outcome,
                "at": r.received_at.isoformat(),
            }
            for r in recent
        ]
    router_ = runtime.services.router
    return {
        "usage": usage,
        "cost_30d_usd": round(sum(u["cost_usd"] for u in usage), 6),
        "calls_30d": sum(u["calls"] for u in usage),
        "failed_30d": sum(u["calls"] - u["ok"] for u in usage),
        "daily_cap_usd": runtime.settings.llm_daily_cost_cap_usd,
        "outcomes": {str(k): v for k, v in outcomes.items()},
        "chains": {p: router_.usable_models(p) for p in ("intent", "extract", "query", "receipt")},
        "recent_messages": recent_json,
    }
