"""Budgets & soft alerts (§12.12): "budget cafes 200" sets a monthly soft limit;
once a category passes 80%, logging something in it adds a one-line nudge."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pocket.channels.base import OutboundMessage
from pocket.core import money
from pocket.core.categorize import CatRef, match_category
from pocket.core.dates import period_range
from pocket.data.models import Budget, Category, Transaction, User

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.wiring import Services

NUDGE_AT = 0.8


def spent(s: Session, user: User, budget: Budget, now=None) -> int:
    rng = period_range(
        "this_month" if budget.period == "month" else "this_week", user.timezone, now
    )
    ids = {budget.category_id} | {
        c.id for c in s.scalars(select(Category).where(Category.parent_id == budget.category_id))
    }
    total = s.scalar(
        select(func.coalesce(func.sum(Transaction.amount_base_minor), 0)).where(
            Transaction.user_id == user.id,
            Transaction.deleted_at.is_(None),
            Transaction.direction == "expense",
            Transaction.category_id.in_(ids),
            Transaction.occurred_at >= rng.start,
            Transaction.occurred_at < rng.end,
        )
    )
    return int(total or 0)


def bar(frac: float, width: int = 10) -> str:
    filled = min(width, round(frac * width))
    return "▓" * filled + "░" * (width - filled)


def status_line(s: Session, user: User, b: Budget, now=None) -> tuple[str, float]:
    used = spent(s, user, b, now)
    frac = used / b.amount_minor if b.amount_minor else 0
    icon = "🔴" if frac >= 1 else "🟠" if frac >= NUDGE_AT else "🟢"
    per = "this month" if b.period == "month" else "this week"
    line = (
        f"{icon} {b.category.full_name}: {money.fmt(used, user.base_currency)} of "
        f"{money.fmt(b.amount_minor, user.base_currency)} {per} {bar(frac)} {round(frac * 100)}%"
    )
    return line, frac


def _find_cat(t: Turn, name: str) -> Category | None:
    cats = t.s.scalars(
        select(Category).where(Category.user_id == t.user.id, Category.archived_at.is_(None))
    ).all()
    c, score = match_category(name, [CatRef(x.id, x.name) for x in cats])
    return next((x for x in cats if c and x.id == c.id and score >= 0.8), None)


async def budget_set(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    cat = _find_cat(t, cmd.args["category"])
    if not cat:
        return [
            OutboundMessage(
                text=f'No category like "{cmd.args["category"]}". Send "categories" to see them.'
            )
        ]
    amount = money.to_minor(Decimal(cmd.args["amount"]), t.base)
    b = t.s.scalars(
        select(Budget).where(
            Budget.user_id == t.user.id,
            Budget.category_id == cat.id,
            Budget.period == cmd.args["period"],
        )
    ).first()
    if b:
        b.amount_minor = amount
    else:
        b = Budget(
            user_id=t.user.id, category_id=cat.id, amount_minor=amount, period=cmd.args["period"]
        )
        t.s.add(b)
    t.s.flush()
    t.s.refresh(b)
    line, _ = status_line(t.s, t.user, b, t.now)
    return [OutboundMessage(text=f"🎯 Budget set.\n{line}")]


async def budget_list(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    rows = t.s.scalars(select(Budget).where(Budget.user_id == t.user.id)).all()
    if not rows:
        return [
            OutboundMessage(
                text='No budgets yet. Try "budget cafes 80" (monthly) or "budget groceries 60/week".'
            )
        ]
    lines = [status_line(t.s, t.user, b, t.now)[0] for b in rows]
    return [OutboundMessage(text="Budgets:\n" + "\n".join(lines))]


async def budget_remove(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    cat = _find_cat(t, cmd.args["category"])
    rows = (
        t.s.scalars(
            select(Budget).where(Budget.user_id == t.user.id, Budget.category_id == cat.id)
        ).all()
        if cat
        else []
    )
    for b in rows:
        t.s.delete(b)
    t.s.flush()
    return [
        OutboundMessage(
            text=f"Removed {len(rows)} budget(s)." if rows else "No budget for that category."
        )
    ]


def nudge(t: Turn, saved: list[Transaction]) -> str | None:
    cat_ids = {x.category_id for x in saved if x.direction == "expense" and x.category_id}
    if not cat_ids:
        return None
    # a budget on the parent covers children too
    parents = {
        c.parent_id
        for c in t.s.scalars(select(Category).where(Category.id.in_(cat_ids)))
        if c.parent_id
    }
    rows = t.s.scalars(
        select(Budget).where(Budget.user_id == t.user.id, Budget.category_id.in_(cat_ids | parents))
    ).all()
    lines = []
    for b in rows:
        line, frac = status_line(t.s, t.user, b, t.now)
        if frac >= NUDGE_AT:
            lines.append(line)
    return "\n".join(lines) or None


def install(services: Services) -> None:
    o = services.orchestrator
    o.extra_commands.update(
        {"budget_set": budget_set, "budgets": budget_list, "budget_remove": budget_remove}
    )
    o.after_commit.append(nudge)
