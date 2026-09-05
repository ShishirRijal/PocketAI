"""Recurring transactions (§12.11).

    every 15th 12.99 spotify
    every month on the 1st rent 650
    weekly on monday 30 cleaning
    every day 3 coffee
    yearly on 03-14 49 domain renewal

Cadence and anchor are parsed deterministically; the "12.99 spotify" part goes
through the normal extraction stage so categories and tags match everything else.
"""

from __future__ import annotations

import logging
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select

from pocket.channels.base import OutboundMessage
from pocket.core import money, render
from pocket.data.db import utcnow
from pocket.data.models import RecurringRule, Transaction, User
from pocket.data.repositories import CategoryRepo, TagRepo, TransactionRepo
from pocket.llm.rules.lexicon import WEEKDAYS

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.runtime import Runtime
    from pocket.wiring import Services

log = logging.getLogger(__name__)

_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))


def parse_schedule(text: str) -> tuple[str, int | None, str] | None:
    """-> (cadence, anchor, remaining text) or None."""
    t = text.strip()
    t = re.sub(r"^(recurring add|repeat)\s+", "", t, flags=re.I)
    low = t.lower()
    pats: list[tuple[str, str]] = [
        (rf"^(?:every|each)\s+(?:week\s+on\s+)?({_WD})\b", "weekly_wd"),
        (rf"^weekly(?:\s+on)?\s+({_WD})\b", "weekly_wd"),
        (
            r"^(?:every|each)\s+(?:month\s+)?(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b(?:\s+of\s+(?:the|every)\s+month)?",
            "monthly",
        ),
        (r"^monthly(?:\s+on)?(?:\s+the)?\s+(\d{1,2})(?:st|nd|rd|th)?\b", "monthly"),
        (r"^(?:every|each)\s+day\b|^daily\b", "daily"),
        (r"^(?:every|each)\s+week\b|^weekly\b", "weekly_now"),
        (r"^(?:every|each)\s+month\b|^monthly\b", "monthly_now"),
        (
            r"^(?:every|each)\s+year\s+on\s+(\d{1,2})[-/](\d{1,2})\b|^yearly\s+on\s+(\d{1,2})[-/](\d{1,2})\b",
            "yearly",
        ),
    ]
    for rx, kind in pats:
        m = re.search(rx, low)
        if not m:
            continue
        rest = t[m.end() :].strip(" ,:-")
        match kind:
            case "weekly_wd":
                return "weekly", WEEKDAYS[m.group(1)], rest
            case "monthly":
                day = int(m.group(1))
                return ("monthly", day, rest) if 1 <= day <= 31 else None
            case "daily":
                return "daily", None, rest
            case "weekly_now":
                return "weekly", None, rest
            case "monthly_now":
                return "monthly", None, rest
            case "yearly":
                mm, dd = (m.group(1) or m.group(3)), (m.group(2) or m.group(4))
                return "yearly", int(mm) * 100 + int(dd), rest
    return None


def _clamp_day(y: int, mth: int, day: int) -> date:
    return date(y, mth, min(day, monthrange(y, mth)[1]))


def next_date(cadence: str, anchor: int | None, after: date, *, inclusive: bool) -> date:
    """First date >= after (inclusive) or > after that matches the schedule."""
    d = after if inclusive else after + timedelta(days=1)
    match cadence:
        case "daily":
            return d
        case "weekly":
            return d + timedelta(days=(anchor - d.weekday()) % 7) if anchor is not None else d
        case "monthly":
            day = anchor or after.day
            cand = _clamp_day(d.year, d.month, day)
            if cand < d:
                nm = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
                cand = _clamp_day(nm.year, nm.month, day)
            return cand
        case "yearly":
            mm, dd = divmod(anchor or (after.month * 100 + after.day), 100)
            cand = _clamp_day(d.year, mm, dd)
            return cand if cand >= d else _clamp_day(d.year + 1, mm, dd)
    raise ValueError(cadence)


def _at_nine(d: date, tz: str) -> datetime:
    return datetime.combine(d, time(9, 0), tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))


def describe(r: RecurringRule) -> str:
    match r.cadence:
        case "daily":
            when = "every day"
        case "weekly":
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            when = f"every {names[r.anchor]}" if r.anchor is not None else "weekly"
        case "monthly":
            when = f"every month on the {r.anchor}{_suffix(r.anchor or 1)}"
        case _:
            mm, dd = divmod(r.anchor or 101, 100)
            when = f"every year on {date(2000, mm, dd):%b %d}"
    what = r.merchant or (r.category.full_name if r.category else "")
    return f"{money.fmt(r.amount_minor, r.currency)} {what} · {when}"


def _suffix(n: int) -> str:
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


async def recurring_add(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    parsed = parse_schedule(cmd.args["text"])
    if not parsed:
        return [
            OutboundMessage(
                text='I couldn\'t read the schedule. Try "every 15th 12.99 spotify" or "weekly on monday 30 cleaning".'
            )
        ]
    cadence, anchor, rest = parsed
    ext = await o.pipeline.extract(rest, o._uctx(t))
    if not ext.transactions:
        return [
            OutboundMessage(
                text=f'What\'s the amount? e.g. "{cmd.args["text"].split()[0]} ... 12.99 spotify"'
            )
        ]
    p = await o.propose_from_extracted(t, ext.transactions[0], rest)
    cat_id = p.category_id
    if cat_id is None and p.new_category:
        cat_id = CategoryRepo(t.s).create(t.user.id, p.new_category).id
    today = t.now.astimezone(ZoneInfo(t.tz)).date()
    first = next_date(cadence, anchor, today, inclusive=True)
    rule = RecurringRule(
        user_id=t.user.id,
        amount_minor=p.amount_minor,
        currency=p.currency,
        direction=p.direction,
        category_id=cat_id,
        merchant=p.merchant,
        note=p.note,
        tags=[n for n, _ in p.tags],
        cadence=cadence,
        anchor=anchor,
        next_run=_at_nine(first, t.tz),
    )
    t.s.add(rule)
    t.s.flush()
    t.s.refresh(rule)
    when = "today" if first == today else f"{first:%a %b %d}"
    return [
        OutboundMessage(
            text=f'🔁 Recurring: {describe(rule)}\nFirst one posts {when}. "recurring" lists them.'
        )
    ]


async def recurring_list(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    rules = _rules(t)
    if not rules:
        return [
            OutboundMessage(
                text='No recurring transactions. Add one like "every 15th 12.99 spotify".'
            )
        ]
    tz = ZoneInfo(t.tz)
    lines = [
        f"{i}. {describe(r)} (next {r.next_run.astimezone(tz):%b %d})"
        for i, r in enumerate(rules, 1)
    ]
    total = sum(r.amount_minor for r in rules if r.cadence == "monthly" and r.currency == t.base)
    tail = f"\nMonthly fixed costs: {money.fmt(total, t.base)}" if total else ""
    return [
        OutboundMessage(
            text="Recurring:\n" + "\n".join(lines) + tail + '\nStop one with "recurring stop 2".'
        )
    ]


async def recurring_remove(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    rules = _rules(t)
    i = cmd.args["index"]
    if not 1 <= i <= len(rules):
        return [OutboundMessage(text='No such recurring item. Send "recurring" to see the list.')]
    rules[i - 1].active = False
    t.s.flush()
    return [OutboundMessage(text=f"⏹️ Stopped: {describe(rules[i - 1])}")]


def _rules(t: Turn) -> list[RecurringRule]:
    return list(
        t.s.scalars(
            select(RecurringRule)
            .where(RecurringRule.user_id == t.user.id, RecurringRule.active.is_(True))
            .order_by(RecurringRule.id)
        ).all()
    )


def post_due_sync(db, now: datetime | None = None) -> list[tuple[int, str]]:
    """Create transactions for every due rule. Returns [(user_id, message)] to send."""
    now = now or utcnow()
    notes: list[tuple[int, str]] = []
    with db.session() as s:
        due = s.scalars(
            select(RecurringRule).where(
                RecurringRule.active.is_(True), RecurringRule.next_run <= now
            )
        ).all()
        for r in due:
            user = s.get(User, r.user_id)
            assert user is not None
            posted = []
            # catch up if the box was down for a while, but never more than 31 posts
            for _ in range(31):
                if r.next_run > now:
                    break
                rate = None
                base_minor = r.amount_minor
                if r.currency != user.base_currency:
                    # recurring posts use the last known rate for that pair from history
                    last = s.scalars(
                        select(Transaction)
                        .where(
                            Transaction.user_id == user.id,
                            Transaction.currency == r.currency,
                            Transaction.fx_rate.is_not(None),
                        )
                        .order_by(Transaction.id.desc())
                    ).first()
                    if last and last.fx_rate:
                        rate = last.fx_rate
                        base_minor = money.convert_minor(
                            r.amount_minor, r.currency, user.base_currency, rate
                        )
                tags = [TagRepo(s).get_or_create(user.id, n) for n in r.tags or []]
                txn = TransactionRepo(s).add(
                    Transaction(
                        user_id=user.id,
                        amount_minor=r.amount_minor,
                        currency=r.currency,
                        amount_base_minor=base_minor,
                        fx_rate=rate,
                        direction=r.direction,
                        category_id=r.category_id,
                        merchant=r.merchant,
                        note=r.note or "recurring",
                        occurred_at=r.next_run,
                        created_at=now,
                    ),
                    tags,
                )
                posted.append(txn)
                d = r.next_run.astimezone(ZoneInfo(user.timezone)).date()
                r.next_run = _at_nine(
                    next_date(r.cadence, r.anchor, d, inclusive=False), user.timezone
                )
                r.last_posted_at = now
            if posted:
                lines = "\n".join(
                    render.txn_line(x, user.base_currency, user.timezone) for x in posted
                )
                notes.append(
                    (user.id, f'🔁 Logged recurring:\n{lines}\n("delete 1" if it didn\'t happen)')
                )
    return notes


async def post_due(rt: Runtime) -> None:
    for user_id, text in post_due_sync(rt.services.db):
        await rt.dispatcher.send_to_user(user_id, OutboundMessage(text=text))


def install(services: Services) -> None:
    services.orchestrator.extra_commands.update(
        {
            "recurring_add": recurring_add,
            "recurring": recurring_list,
            "recurring_remove": recurring_remove,
        }
    )
