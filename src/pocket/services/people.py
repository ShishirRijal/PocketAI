"""People as mini-profiles (§12.16): tags with kind=person. `person arjun` shows
what you've spent together this year, where, and where the lending stands."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import select

from pocket.channels.base import OutboundMessage
from pocket.core import money
from pocket.core.dates import humanize_when, period_range
from pocket.core.directions import LENDING
from pocket.data.models import Tag, Transaction, TransactionTag
from pocket.data.repositories import normalize_tag
from pocket.services.lending import balances

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.wiring import Services


async def person(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    name = normalize_tag(cmd.args["name"])
    tag = t.s.scalars(select(Tag).where(Tag.user_id == t.user.id, Tag.name == name)).first()
    if tag is None:
        people = t.s.scalars(
            select(Tag.name)
            .where(Tag.user_id == t.user.id, Tag.kind == "person")
            .order_by(Tag.name)
        ).all()
        known = ", ".join(people[:15]) or "nobody yet"
        return [OutboundMessage(text=f"I don't know {cmd.args['name']}. People I know: {known}")]
    year = period_range("this_year", t.tz, t.now)
    rows = t.s.scalars(
        select(Transaction)
        .join(TransactionTag, TransactionTag.transaction_id == Transaction.id)
        .where(
            TransactionTag.tag_id == tag.id,
            Transaction.user_id == t.user.id,
            Transaction.deleted_at.is_(None),
        )
        .order_by(Transaction.occurred_at.desc())
    ).all()
    spend = [r for r in rows if r.direction == "expense"]
    this_year = [r for r in spend if r.occurred_at >= year.start]
    by_cat: dict[str, int] = defaultdict(int)
    for r in this_year:
        by_cat[r.category.full_name if r.category else "Uncategorized"] += r.amount_base_minor
    total = sum(r.amount_base_minor for r in this_year)
    lines = [f"👤 {name.capitalize()}"]
    if this_year:
        lines.append(
            f"Together in {year.label}: {money.fmt(total, t.base)} across {len(this_year)}"
        )
        top = sorted(by_cat.items(), key=lambda kv: -kv[1])[:3]
        lines.append("Mostly: " + ", ".join(f"{k} {money.fmt(v, t.base)}" for k, v in top))
    if spend:
        last = spend[0]
        what = last.merchant or (last.category.name if last.category else "")
        lines.append(
            f"Last time: {humanize_when(last.occurred_at, t.tz, t.now)}, {money.fmt(last.amount_minor, last.currency)} {what}".rstrip()
        )
    if any(r.direction in LENDING for r in rows):
        bal = balances(t.s, t.user.id).get(name, 0)
        if bal > 0:
            lines.append(f"🤝 Owes you {money.fmt(bal, t.base)}")
        elif bal < 0:
            lines.append(f"🤝 You owe them {money.fmt(-bal, t.base)}")
        else:
            lines.append("🤝 All square")
    if len(lines) == 1:
        lines.append("Nothing logged with them yet.")
    return [OutboundMessage(text="\n".join(lines))]


def install(services: Services) -> None:
    services.orchestrator.extra_commands["person"] = person
