"""Savings goals.

goal japan 2000 by march     -> create (target in base currency, optional deadline)
save 200 japan               -> contribution (a `transfer`, so it isn't spending)
goals                        -> progress + what's needed per month to make it
goal done japan              -> mark reached / stop tracking
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pocket.channels.base import OutboundMessage
from pocket.core import money
from pocket.core.dates import find_date
from pocket.data.db import utcnow
from pocket.data.models import Goal, Tag, Transaction, TransactionTag, User
from pocket.data.repositories import TagRepo, TransactionRepo, normalize_tag
from pocket.llm.rules.lexicon import MONTHS

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.wiring import Services


def _tag(slug: str) -> str:
    return f"goal-{slug}"


def parse_deadline(text: str, today: date) -> date | None:
    t = text.lower().strip()
    if m := re.fullmatch(r"(?:end of )?(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", t):
        month = MONTHS[m.group(1)]
        year = (
            int(m.group(2))
            if m.group(2)
            else (today.year if month >= today.month else today.year + 1)
        )
        return (date(year, month, 1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    if m := re.fullmatch(r"(?:in\s+)?(\d{1,2})\s+months?", t):
        return today + timedelta(days=30 * int(m.group(1)))
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    d, _ = find_date(t, today)
    return d


def saved(s: Session, user_id: int, slug: str) -> int:
    total = s.scalar(
        select(func.coalesce(func.sum(Transaction.amount_base_minor), 0))
        .join(TransactionTag, TransactionTag.transaction_id == Transaction.id)
        .join(Tag, Tag.id == TransactionTag.tag_id)
        .where(
            Transaction.user_id == user_id,
            Transaction.deleted_at.is_(None),
            Tag.user_id == user_id,
            Tag.name == _tag(slug),
        )
    )
    return int(total or 0)


def progress(s: Session, user: User, g: Goal, today: date) -> dict[str, Any]:
    have = saved(s, user.id, g.slug)
    left = max(0, g.target_minor - have)
    per_month = None
    if g.due and left:
        due = g.due.astimezone(ZoneInfo(user.timezone)).date()
        months = max(
            1,
            (due.year - today.year) * 12
            + due.month
            - today.month
            + (1 if due.day >= today.day else 0),
        )
        per_month = -(-left // months)
    return {
        "id": g.id,
        "name": g.name,
        "slug": g.slug,
        "target_minor": g.target_minor,
        "saved_minor": have,
        "left_minor": left,
        "due": g.due.astimezone(ZoneInfo(user.timezone)).date().isoformat() if g.due else None,
        "per_month_minor": per_month,
    }


def _active(t: Turn) -> list[Goal]:
    return list(
        t.s.scalars(
            select(Goal).where(Goal.user_id == t.user.id, Goal.done_at.is_(None)).order_by(Goal.id)
        ).all()
    )


def _find(t: Turn, name: str) -> Goal | None:
    slug = normalize_tag(name)
    goals = _active(t)
    return next((g for g in goals if g.slug == slug), None) or next(
        (g for g in goals if g.slug.startswith(slug) or slug.startswith(g.slug)), None
    )


def _line(p: dict[str, Any], cur: str) -> str:
    frac = p["saved_minor"] / p["target_minor"] if p["target_minor"] else 0
    filled = min(10, round(frac * 10))
    bits = [
        f"🎯 {p['name']}: {money.fmt(p['saved_minor'], cur)} of {money.fmt(p['target_minor'], cur)} "
        f"{'▓' * filled}{'░' * (10 - filled)} {round(frac * 100)}%"
    ]
    if p["left_minor"] == 0:
        bits.append("reached 🎉")
    elif p["per_month_minor"]:
        bits.append(f"{money.fmt(p['per_month_minor'], cur)}/month to make {p['due']}")
    return " · ".join(bits)


async def goal_add(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    name = cmd.args["name"].strip()
    slug = normalize_tag(name)
    today = t.now.astimezone(ZoneInfo(t.tz)).date()
    due = parse_deadline(cmd.args["by"], today) if cmd.args.get("by") else None
    if cmd.args.get("by") and due is None:
        return [
            OutboundMessage(
                text=f'I couldn\'t read the deadline "{cmd.args["by"]}". Try "by march" or "in 6 months".'
            )
        ]
    target = money.to_minor(Decimal(cmd.args["amount"]), t.base)
    g = t.s.scalars(select(Goal).where(Goal.user_id == t.user.id, Goal.slug == slug)).first()
    if g:
        g.target_minor, g.done_at = target, None
        if due:
            g.due = datetime.combine(due, time(12), tzinfo=ZoneInfo(t.tz))
    else:
        g = Goal(
            user_id=t.user.id, name=name.title() if name.islower() else name, slug=slug, target_minor=target,
            due=datetime.combine(due, time(12), tzinfo=ZoneInfo(t.tz)) if due else None,
        )  # fmt: skip
        t.s.add(g)
    t.s.flush()
    return [
        OutboundMessage(
            text=_line(progress(t.s, t.user, g, today), t.base)
            + f'\nAdd to it with "save 100 {slug}".'
        )
    ]


async def goal_save(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    g = _find(t, cmd.args["name"])
    if (
        g is None
        and "goal" not in (cmd.args.get("text") or "").lower()
        and not cmd.args.get("withdraw")
    ):
        # "saved 20 on groceries" isn't about a goal; let the normal pipeline have it
        return await o._add(t, cmd.args["text"])
    if g is None:
        names = ", ".join(x.slug for x in _active(t)) or "none yet"
        return [
            OutboundMessage(
                text=f'No goal called "{cmd.args["name"]}" (goals: {names}). Create one: "goal japan 2000 by march".'
            )
        ]
    amount = money.to_minor(Decimal(cmd.args["amount"]), t.base)
    sign = -1 if cmd.args.get("withdraw") else 1
    txn = TransactionRepo(t.s).add(
        Transaction(
            user_id=t.user.id, amount_minor=amount, currency=t.base, amount_base_minor=sign * amount,
            direction="transfer", note=f"{'withdrawn from' if sign < 0 else 'saved for'} {g.name}",
            occurred_at=t.now, created_at=t.now, raw_message_id=t.raw_message_id,
        ),
        [TagRepo(t.s).get_or_create(t.user.id, _tag(g.slug), "other")],
    )  # fmt: skip
    from pocket.core.session import LastAction

    t.session.last_action = LastAction(kind="add", transaction_ids=[txn.id], at=t.now)
    today = t.now.astimezone(ZoneInfo(t.tz)).date()
    p = progress(t.s, t.user, g, today)
    head = "💰 Saved" if sign > 0 else "💸 Took out"
    return [
        OutboundMessage(
            text=f"{head} {money.fmt(amount, t.base)} {'for' if sign > 0 else 'from'} {g.name}\n{_line(p, t.base)}"
        )
    ]


async def goals_list(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    goals = _active(t)
    if not goals:
        return [OutboundMessage(text='No goals yet. Try "goal japan 2000 by march".')]
    today = t.now.astimezone(ZoneInfo(t.tz)).date()
    return [
        OutboundMessage(
            text="\n".join(_line(progress(t.s, t.user, g, today), t.base) for g in goals)
        )
    ]


async def goal_done(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    g = _find(t, cmd.args["name"])
    if g is None:
        return [OutboundMessage(text="No such goal.")]
    g.done_at = utcnow()
    return [
        OutboundMessage(text=f"✅ Closed goal {g.name}. The savings entries stay in your history.")
    ]


def install(services: Services) -> None:
    services.orchestrator.extra_commands.update(
        {"goal_add": goal_add, "goal_save": goal_save, "goals": goals_list, "goal_done": goal_done}
    )
