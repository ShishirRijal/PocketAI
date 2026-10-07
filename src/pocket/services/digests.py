"""Weekly self-digest (§9, §12.6): Sunday 20:00 in the user's timezone, a
recap of the week plus anything low-confidence worth reviewing.

The numbers are computed here; an LLM (summarizer chain) only rephrases them
into a friendlier paragraph, and if that fails the plain version goes out.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pocket.channels.base import OutboundMessage
from pocket.core import money
from pocket.core.dates import local_now, period_range
from pocket.data.db import Database, utcnow
from pocket.data.models import Transaction, User

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.runtime import Runtime
    from pocket.wiring import Services

log = logging.getLogger(__name__)


def weekly_data(
    db: Database, user_id: int, now: datetime | None = None, span: str = "week"
) -> dict[str, Any]:
    """Numbers for a recap. span="week": the last 7 days vs the 7 before;
    span="month": last calendar month vs the one before it."""
    with db.session() as s:
        user = s.get(User, user_id)
        assert user is not None
        cur = user.base_currency
        if span == "month":
            this = period_range("last_month", user.timezone, now)
            prev_start = period_range(
                "last_month", user.timezone, this.start - timedelta(seconds=1)
            ).start
        else:
            this = period_range("last_7_days", user.timezone, now)
            prev_start = this.start - timedelta(days=7)
        rows = s.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.deleted_at.is_(None),
                Transaction.occurred_at >= prev_start,
                Transaction.occurred_at < this.end,
            )
        ).all()
        week = [r for r in rows if r.occurred_at >= this.start]
        prev = [r for r in rows if r.occurred_at < this.start]
        spent = sum(r.amount_base_minor for r in week if r.direction == "expense")
        prev_spent = sum(r.amount_base_minor for r in prev if r.direction == "expense")
        income = sum(r.amount_base_minor for r in week if r.direction == "income")
        cats: dict[str, int] = defaultdict(int)
        prev_cats: dict[str, int] = defaultdict(int)
        for r in week:
            if r.direction == "expense":
                cats[r.category.full_name if r.category else "Uncategorized"] += r.amount_base_minor
        for r in prev:
            if r.direction == "expense":
                prev_cats[r.category.full_name if r.category else "Uncategorized"] += (
                    r.amount_base_minor
                )
        top = sorted(cats.items(), key=lambda kv: kv[1], reverse=True)[:4]
        biggest = max(
            (r for r in week if r.direction == "expense"),
            key=lambda r: r.amount_base_minor,
            default=None,
        )
        review = [
            r for r in week if r.llm_confidence is not None and float(r.llm_confidence) < 0.85
        ]
        jumps = [
            (k, v, prev_cats.get(k, 0))
            for k, v in cats.items()
            if prev_cats.get(k, 0)
            and v > 1.5 * prev_cats[k]
            and v - prev_cats[k] > money.to_minor(20, cur)
        ]
        return {
            "span": span,
            "label": this.label,
            "currency": cur,
            "spent": money.fmt(spent, cur),
            "spent_minor": spent,
            "previous_week_spent": money.fmt(prev_spent, cur),
            "change_pct": round(100 * (spent - prev_spent) / prev_spent) if prev_spent else None,
            "income": money.fmt(income, cur) if income else None,
            "income_minor": income,
            "transactions": len([r for r in week if r.direction == "expense"]),
            "top_categories": [{"name": k, "amount": money.fmt(v, cur)} for k, v in top],
            "biggest": {
                "amount": money.fmt(biggest.amount_minor, biggest.currency),
                "what": biggest.merchant or (biggest.category.name if biggest.category else ""),
            }
            if biggest
            else None,
            "category_jumps": [
                {"name": k, "this_week": money.fmt(v, cur), "last_week": money.fmt(p, cur)}
                for k, v, p in jumps
            ],
            "to_review": [
                f"{money.fmt(r.amount_minor, r.currency)} {r.merchant or (r.category.name if r.category else '')}".strip()
                for r in review[:5]
            ],
        }


def plain_digest(d: dict[str, Any]) -> str:
    month = d.get("span") == "month"
    if not d["transactions"]:
        what = f"in {d.get('label', 'last month')}" if month else "this week"
        return f"📊 Recap: nothing logged {what}. Quiet, or did I miss some? 🙂"
    head = f"📅 {d['label']}" if month else "📊 Your week"
    lines = [f"{head}: {d['spent']} across {d['transactions']} transactions"]
    if d["change_pct"] is not None:
        arrow = "▲" if d["change_pct"] > 0 else "▼"
        lines[0] += (
            f" ({arrow}{abs(d['change_pct'])}% vs {'the month before' if month else 'last week'})"
        )
    if d["income"]:
        lines.append(f"Income: {d['income']}")
    if d["top_categories"]:
        lines.append("Top: " + ", ".join(f"{c['name']} {c['amount']}" for c in d["top_categories"]))
    if d["biggest"]:
        lines.append(f"Biggest: {d['biggest']['amount']} {d['biggest']['what']}".rstrip())
    for j in d["category_jumps"]:
        lines.append(f"Heads up: {j['name']} {j['this_week']} (last week {j['last_week']})")
    if d.get("income") and month:
        lines.append(
            f"Saved: {money.fmt(d['income_minor'] - d['spent_minor'], d['currency'], sign=True)}"
        )
    if d["to_review"]:
        lines.append(
            "Worth a look (low confidence): " + "; ".join(d["to_review"]) + ' — send "review"'
        )
    return "\n".join(lines)


async def build_digest(rt_or_orch: Any, user_id: int, span: str = "week") -> str:
    orch = getattr(rt_or_orch, "services", None)
    orch = orch.orchestrator if orch else rt_or_orch
    d = weekly_data(orch.db, user_id, span=span, now=orch.clock())
    plain = plain_digest(d)
    if not d["transactions"]:
        return plain
    try:
        with orch.db.session() as s:
            user = s.get(User, user_id)
            u = orch.user_context(s, user)
        text = await orch.pipeline.summarize(d, u)
        # the model rephrases; if it drops the headline number, don't trust it
        if d["spent"].replace(",", "") not in text.replace(",", ""):
            return plain
        return text.strip()
    except Exception as e:
        log.info("digest summarizer unavailable (%s), sending plain version", e)
        return plain


_sent: dict[int, str] = {}


async def send_digest(rt: Runtime, user_id: int, *, force: bool = False) -> str | None:
    week_key = utcnow().strftime("%G-W%V")
    if not force and _sent.get(user_id) == week_key:
        return None
    text = await build_digest(rt, user_id)
    await rt.dispatcher.send_to_user(user_id, OutboundMessage(text=text))
    _sent[user_id] = week_key
    return text


_sent_monthly: dict[int, str] = {}


async def send_due_digests(rt: Runtime) -> None:
    """Runs hourly. Weekly: Sunday 20:xx local. Monthly: the 1st, 09:xx local."""
    s_ = rt.settings
    with rt.services.db.session() as s:
        users = [(u.id, u.timezone) for u in s.scalars(select(User)).all()]
    for uid, tz in users:
        ln = local_now(tz)
        try:
            if ln.weekday() == s_.digest_weekday and ln.hour == s_.digest_hour:
                await send_digest(rt, uid)
            month_key = ln.strftime("%Y-%m")
            if ln.day == 1 and ln.hour == 9 and _sent_monthly.get(uid) != month_key:
                text = await build_digest(rt, uid, span="month")
                await rt.dispatcher.send_to_user(uid, OutboundMessage(text=text))
                _sent_monthly[uid] = month_key
        except Exception:
            log.exception("digest failed for user %s", uid)


async def digest_cmd(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    t.s.flush()
    return [
        OutboundMessage(text=await build_digest(o, t.user.id, span=cmd.args.get("span", "week")))
    ]


def install(services: Services) -> None:
    services.orchestrator.extra_commands["digest"] = digest_cmd
