"""Runs a QueryPlan. The LLM only fills in the plan; this is deterministic.

SQL does the filtering (user, range, direction, category tree, merchant, tag);
Python does the grouping because weekday/day buckets depend on the user's
timezone and it keeps the code identical on sqlite and postgres.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pocket.core import money
from pocket.core.categorize import CatRef, match_category
from pocket.core.dates import Range, humanize_when, period_range
from pocket.data.models import Category, Tag, Transaction, TransactionTag, User
from pocket.llm.schemas import QueryPlan


@dataclass
class QueryResult:
    title: str
    lines: list[str] = field(default_factory=list)
    total_minor: int = 0
    count: int = 0
    currency: str = "EUR"
    empty: bool = False
    data: dict = field(default_factory=dict)

    def text(self) -> str:
        return "\n".join([self.title, *self.lines]) if self.lines else self.title


def _category_ids(s: Session, user_id: int, name: str) -> tuple[list[int], str] | None:
    cats = s.scalars(select(Category).where(Category.user_id == user_id)).all()
    c, score = match_category(name, [CatRef(x.id, x.name) for x in cats])
    if not c or score < 0.8:
        return None
    ids = {c.id}
    # include children ("Food" includes "Food/Groceries" style sub-categories)
    changed = True
    while changed:
        changed = False
        for x in cats:
            if x.parent_id in ids and x.id not in ids:
                ids.add(x.id)
                changed = True
    return sorted(ids), c.name


def fetch(
    s: Session, user: User, plan: QueryPlan, rng: Range
) -> tuple[list[Transaction], list[str]]:
    """Returns matching transactions plus human-readable filter labels."""
    conds = [
        Transaction.user_id == user.id,
        Transaction.deleted_at.is_(None),
        Transaction.occurred_at >= rng.start,
        Transaction.occurred_at < rng.end,
    ]
    labels: list[str] = []
    if plan.direction != "any":
        conds.append(Transaction.direction == plan.direction)
    wanted = [c for c in [plan.category, *plan.categories] if c]
    if wanted:
        all_ids: set[int] = set()
        names: list[str] = []
        for cname in dict.fromkeys(wanted):
            found = _category_ids(s, user.id, cname)
            if found is None:
                continue
            ids, name = found
            all_ids.update(ids)
            names.append(name)
        if not all_ids:
            return [], [f"category '{', '.join(wanted)}' (not found)"]
        conds.append(Transaction.category_id.in_(sorted(all_ids)))
        labels.append(" + ".join(names))
    if plan.merchant:
        conds.append(func.lower(Transaction.merchant) == plan.merchant.lower())
        labels.append(f"at {plan.merchant}")
    stmt = select(Transaction).where(*conds)
    if plan.tag:
        tag_name = plan.tag.lstrip("#").lower()
        stmt = (
            stmt.join(TransactionTag, TransactionTag.transaction_id == Transaction.id)
            .join(Tag, Tag.id == TransactionTag.tag_id)
            .where(Tag.name == tag_name)
        )
        labels.append(f"#{tag_name}")
    rows = list(s.scalars(stmt.order_by(Transaction.occurred_at)).unique().all())
    tz = ZoneInfo(user.timezone)
    if plan.weekdays_only:
        rows = [r for r in rows if r.occurred_at.astimezone(tz).weekday() < 5]
        labels.append("weekdays")
    if plan.weekends_only:
        rows = [r for r in rows if r.occurred_at.astimezone(tz).weekday() >= 5]
        labels.append("weekends")
    return rows, labels


def _top(counter: dict[str, int], counts: Counter, cur: str, limit: int) -> list[str]:
    items = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [f"{i}. {k}: {money.fmt(v, cur)} ({counts[k]}×)" for i, (k, v) in enumerate(items, 1)]


def execute(s: Session, user: User, plan: QueryPlan, now: datetime | None = None) -> QueryResult:
    rng = period_range(plan.period, user.timezone, now, plan.start_date, plan.end_date)
    rows, labels = fetch(s, user, plan, rng)
    cur = user.base_currency
    total = sum(r.amount_base_minor for r in rows)
    n = len(rows)
    period_label = (
        f"{rng.label} (so far)" if rng.so_far and plan.period not in ("all_time",) else rng.label
    )
    what = " · ".join(labels)
    head = f"{period_label}{' · ' + what if what else ''}"
    noun = "income" if plan.direction == "income" else "spent"

    if n == 0:
        return QueryResult(title=f"{head}: nothing {noun}.", empty=True, currency=cur)

    res = QueryResult(title="", total_minor=total, count=n, currency=cur)
    tz = user.timezone

    match plan.kind:
        case "total":
            res.title = (
                f"{head}: {money.fmt(total, cur)} {noun} across {n} transaction{'s' * (n != 1)}."
            )
            if not plan.merchant:
                by_m: dict[str, int] = defaultdict(int)
                cnt: Counter = Counter()
                for r in rows:
                    if r.merchant:
                        by_m[r.merchant] += r.amount_base_minor
                        cnt[r.merchant] += 1
                if len(by_m) > 1:
                    top = sorted(by_m.items(), key=lambda kv: kv[1], reverse=True)[:3]
                    res.lines.append(
                        "Top merchants: "
                        + ", ".join(f"{k} {money.fmt(v, cur)}" for k, v in top)
                        + "."
                    )
        case "count":
            res.title = f"{head}: {n} transaction{'s' * (n != 1)} ({money.fmt(total, cur)})."
        case "average":
            res.title = (
                f"{head}: {money.fmt(round(total / n), cur)} on average over {n} transactions."
            )
        case "daily_average":
            end = min(rng.end, (now or datetime.now(rng.start.tzinfo)))
            days = max(1, (end - rng.start).days + (1 if (end - rng.start).seconds else 0))
            res.title = f"{head}: {money.fmt(round(total / days), cur)} per day ({money.fmt(total, cur)} over {days} days)."
        case "largest":
            top_rows = sorted(rows, key=lambda r: r.amount_base_minor, reverse=True)[: plan.limit]
            res.title = f"{head}: biggest {len(top_rows)}"
            res.lines = [_row_line(i, r, tz, cur) for i, r in enumerate(top_rows, 1)]
        case "list":
            shown = sorted(rows, key=lambda r: r.occurred_at, reverse=True)[: max(plan.limit, 10)]
            res.title = f"{head}: {money.fmt(total, cur)} across {n}"
            res.lines = [_row_line(i, r, tz, cur) for i, r in enumerate(shown, 1)]
            if n > len(shown):
                res.lines.append(f"…and {n - len(shown)} more")
        case "top_merchants":
            agg: dict[str, int] = defaultdict(int)
            cnt = Counter()
            for r in rows:
                key = r.merchant or "(no merchant)"
                agg[key] += r.amount_base_minor
                cnt[key] += 1
            res.title = f"{head}: top merchants ({money.fmt(total, cur)} total)"
            res.lines = _top(agg, cnt, cur, plan.limit)
        case "top_tags":
            agg = defaultdict(int)
            cnt = Counter()
            for r in rows:
                for t in r.tags:
                    agg[f"#{t.name}"] += r.amount_base_minor
                    cnt[f"#{t.name}"] += 1
            res.title = f"{head}: top tags"
            res.lines = _top(agg, cnt, cur, plan.limit)
        case "top_categories" | "breakdown":
            group = plan.group_by or "category"
            agg = defaultdict(int)
            cnt = Counter()
            zone = ZoneInfo(tz)
            for r in rows:
                local = r.occurred_at.astimezone(zone)
                if group == "category":
                    key = r.category.full_name if r.category else "Uncategorized"
                elif group == "merchant":
                    key = r.merchant or "(no merchant)"
                elif group == "tag":
                    keys = [f"#{t.name}" for t in r.tags] or ["(untagged)"]
                    for k in keys:
                        agg[k] += r.amount_base_minor
                        cnt[k] += 1
                    continue
                elif group == "day":
                    key = local.strftime("%a %b %d")
                elif group == "weekday":
                    key = local.strftime("%A")
                else:
                    key = local.strftime("%B %Y")
                agg[key] += r.amount_base_minor
                cnt[key] += 1
            res.title = f"{head}: {money.fmt(total, cur)} by {group}"
            if group in ("day", "month"):
                ordered = list(agg.items())  # rows are chronological already
                res.lines = [f"{k}: {money.fmt(v, cur)}" for k, v in ordered][-31:]
            else:
                items = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[
                    : max(plan.limit, 8)
                ]
                res.lines = [
                    f"{k}: {money.fmt(v, cur)} ({round(100 * v / total) if total else 0}%)"
                    for k, v in items
                ]
    res.data = {"period": rng.label, "total_minor": total, "count": n}
    return res


def _row_line(i: int, r: Transaction, tz: str, base: str) -> str:
    amt = money.fmt(r.amount_minor, r.currency)
    if r.currency != base:
        amt += f" (~{money.fmt(r.amount_base_minor, base)})"
    cat = r.category.full_name if r.category else "Uncategorized"
    bits = [amt, cat]
    if r.merchant:
        bits.append(r.merchant)
    bits.append(humanize_when(r.occurred_at, tz))
    return f"{i}. " + " · ".join(bits)
