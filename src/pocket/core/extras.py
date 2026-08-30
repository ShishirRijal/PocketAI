"""Secondary commands: categories, tags, settings, cost, review, history.

Kept out of orchestrator.py so that file stays about the main flow. Each
handler is `async (orch, turn, cmd) -> list[OutboundMessage]`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from pocket.channels.base import OutboundMessage
from pocket.core import money, render
from pocket.core.categorize import CatRef, match_category
from pocket.core.dates import humanize_when, period_range
from pocket.data.models import Category, Tag, Transaction, TransactionTag, UserIdentity
from pocket.data.repositories import CategoryRepo, LLMCallRepo, TransactionRepo

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn


def _out(text: str) -> list[OutboundMessage]:
    return [OutboundMessage(text=text)]


def _find_category(t: Turn, name: str) -> Category | None:
    cats = CategoryRepo(t.s).active(t.user.id)
    c, score = match_category(name, [CatRef(x.id, x.name) for x in cats])
    if c and score >= 0.85:
        return next(x for x in cats if x.id == c.id)
    return None


async def categories(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    rng = period_range("this_month", t.tz, t.now)
    spent = dict(
        t.s.execute(
            select(Transaction.category_id, func.sum(Transaction.amount_base_minor))
            .where(
                Transaction.user_id == t.user.id,
                Transaction.deleted_at.is_(None),
                Transaction.direction == "expense",
                Transaction.occurred_at >= rng.start,
            )
            .group_by(Transaction.category_id)
        ).all()
    )
    cats = CategoryRepo(t.s).active(t.user.id)
    lines = []
    for c in sorted(cats, key=lambda c: (-(spent.get(c.id) or 0), c.full_name)):
        amt = spent.get(c.id)
        lines.append(f"• {c.full_name}" + (f" — {money.fmt(amt, t.base)}" if amt else ""))
    return _out(f"Your categories ({rng.label} spend):\n" + "\n".join(lines))


async def category_add(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    name = cmd.args["name"].strip().strip('"')
    parent_id = None
    if "/" in name:
        parent, name = [x.strip() for x in name.split("/", 1)]
        parent_id = CategoryRepo(t.s).create(t.user.id, parent).id
    c = CategoryRepo(t.s).create(t.user.id, name, parent_id=parent_id)
    return _out(f'➕ Category "{c.full_name}" ready.')


async def category_rename(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    c = _find_category(t, cmd.args["old"])
    if not c:
        return _out(f'No category called "{cmd.args["old"]}".')
    new = cmd.args["new"].strip().strip('"')
    if CategoryRepo(t.s).by_name(t.user.id, new):
        return _out(f'"{new}" already exists. Use "category merge {c.name} into {new}" instead.')
    old = c.name
    CategoryRepo(t.s).rename(c, new)
    return _out(f"✏️ Renamed {old} → {new}.")


async def category_archive(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    c = _find_category(t, cmd.args["name"])
    if not c:
        return _out(f'No category called "{cmd.args["name"]}".')
    CategoryRepo(t.s).archive(c)
    return _out(f"🗄️ Archived {c.name}. Old transactions keep it; new ones won't use it.")


async def category_merge(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    src, dst = _find_category(t, cmd.args["src"]), _find_category(t, cmd.args["dst"])
    if not src or not dst or src.id == dst.id:
        return _out('Couldn\'t find both categories. Send "categories" to see names.')
    repo = TransactionRepo(t.s)
    rows = t.s.scalars(
        select(Transaction).where(
            Transaction.user_id == t.user.id, Transaction.category_id == src.id
        )
    ).all()
    for r in rows:
        repo.update(
            r,
            {"category_id": dst.id},
            changed_by="user",
            reason=f"merge {src.name} into {dst.name}",
        )
    CategoryRepo(t.s).archive(src)
    return _out(
        f"🔀 Moved {len(rows)} transactions from {src.name} into {dst.name} and archived {src.name}."
    )


async def tags(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    rows = t.s.execute(
        select(Tag.name, Tag.kind, func.count(TransactionTag.transaction_id))
        .join(TransactionTag, TransactionTag.tag_id == Tag.id)
        .join(Transaction, Transaction.id == TransactionTag.transaction_id)
        .where(Tag.user_id == t.user.id, Transaction.deleted_at.is_(None))
        .group_by(Tag.id)
        .order_by(func.count(TransactionTag.transaction_id).desc())
        .limit(25)
    ).all()
    if not rows:
        return _out("No tags yet.")
    kinds = {"person": "👤", "merchant": "🏪", "place": "📍", "activity": "•"}
    return _out(
        "Top tags:\n" + "\n".join(f"{kinds.get(k or '', '•')} #{n} ({c})" for n, k, c in rows)
    )


async def cost(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    rng = period_range(cmd.args.get("period", "last_7_days"), t.tz, t.now)
    usage = LLMCallRepo(t.s).usage_by_day_model(rng.start)
    if not usage:
        return _out(f"No LLM calls {rng.label.lower()}.")
    total = sum(u["cost_usd"] for u in usage)
    calls = sum(u["calls"] for u in usage)
    by_model: dict[str, list[float]] = {}
    for u in usage:
        m = by_model.setdefault(u["model"], [0, 0, 0])
        m[0] += u["calls"]
        m[1] += u["cost_usd"]
        m[2] += u["calls"] - u["ok"]
    lines = [
        f"• {m}: {int(c)} calls, ${cost:.4f}" + (f", {int(f)} failed" if f else "")
        for m, (c, cost, f) in sorted(by_model.items(), key=lambda kv: -kv[1][0])
    ]
    today = LLMCallRepo(t.s).cost_since(period_range("today", t.tz, t.now).start)
    cap = o.settings.llm_daily_cost_cap_usd
    return _out(
        f"🧾 LLM usage, {rng.label}: {calls} calls, ${total:.4f}\n"
        + "\n".join(lines)
        + f"\nToday: ${today:.4f} of ${cap:.2f} daily cap"
    )


async def settings_cmd(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    ids = t.s.scalars(select(UserIdentity).where(UserIdentity.user_id == t.user.id)).all()
    n = t.s.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.user_id == t.user.id, Transaction.deleted_at.is_(None)
        )
    )
    chans = ", ".join(f"{i.channel}" for i in ids)
    return _out(
        f"⚙️ Base currency: {t.base}\nTimezone: {t.tz}\nChannels: {chans}\n"
        f"Digest goes to: {t.user.primary_channel or '-'}\nTransactions logged: {n}"
    )


async def set_currency(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    cur = cmd.args["currency"]
    if cur == t.base:
        return _out(f"Base currency is already {cur}.")
    try:
        await o.fx.rate(cur, "EUR")
    except Exception:
        return _out(f"I don't know the currency {cur}.")
    old = t.base
    t.user.base_currency = cur
    t.s.flush()
    return _out(
        f"💱 Base currency {old} → {cur}. New transactions convert to {cur}; "
        "existing totals stay in the currency they were recorded against."
    )


async def set_timezone(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    raw = cmd.args["tz"].strip()
    aliases = {
        "tallinn": "Europe/Tallinn", "porto": "Europe/Lisbon", "lisbon": "Europe/Lisbon",
        "tampere": "Europe/Helsinki", "helsinki": "Europe/Helsinki",
        "kathmandu": "Asia/Kathmandu", "nepal": "Asia/Kathmandu",
    }  # fmt: skip
    tz = aliases.get(raw.lower(), raw)
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return _out(f'Unknown timezone "{raw}". Try e.g. Europe/Lisbon.')
    t.user.timezone = tz
    t.s.flush()
    return _out(f"🕑 Timezone set to {tz}.")


async def review(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    since = t.now - timedelta(days=7)
    rows = t.s.scalars(
        select(Transaction)
        .where(
            Transaction.user_id == t.user.id,
            Transaction.deleted_at.is_(None),
            Transaction.created_at >= since,
            Transaction.llm_confidence < o.settings.confidence_commit,
        )
        .order_by(Transaction.created_at.desc())
        .limit(10)
    ).all()
    if not rows:
        return _out("👌 Nothing shaky from the last 7 days.")
    msg = o._show_list(t, list(rows), "These were low-confidence parses, worth a look:")
    msg.text += '\nFix with e.g. "edit 2 category groceries".'
    return [msg]


async def history(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    rows = o._numbered(t)
    idx = cmd.args["index"]
    if not 1 <= idx <= len(rows):
        return _out('Which one? Send "edit" to see the recent list.')
    txn = rows[idx - 1]
    versions = TransactionRepo(t.s).versions(txn.id)
    lines = [
        f"🕘 {render.txn_line(txn, t.base, t.tz)}",
        f"created {humanize_when(txn.created_at, t.tz, t.now)}",
    ]
    for v in versions:
        fields = ", ".join(v.diff)
        lines.append(
            f"• {humanize_when(v.changed_at, t.tz, t.now)} by {v.changed_by}: {fields} ({v.reason or '-'})"
        )
    return _out("\n".join(lines))


def register(o: Orchestrator) -> None:
    o.extra_commands.update(
        {
            "categories": categories,
            "category_add": category_add,
            "category_rename": category_rename,
            "category_archive": category_archive,
            "category_merge": category_merge,
            "tags": tags,
            "cost": cost,
            "settings": settings_cmd,
            "set_currency": set_currency,
            "set_timezone": set_timezone,
            "review": review,
            "history": history,
        }
    )
