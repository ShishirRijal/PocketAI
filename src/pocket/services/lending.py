"""Lending tracker (§12.13): who owes whom, from lent/borrowed/got_back/paid_back
transactions and their person tags. "owes" shows the balances."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from pocket.channels.base import OutboundMessage
from pocket.core import money
from pocket.core.directions import LENDING, OWED_SIGN
from pocket.data.models import Transaction

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.wiring import Services


def balances(s: Session, user_id: int) -> dict[str, int]:
    """person -> base-currency minor units they owe me (negative: I owe them)."""
    rows = s.scalars(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.deleted_at.is_(None),
            Transaction.direction.in_(LENDING),
        )
    ).all()
    out: dict[str, int] = defaultdict(int)
    for t in rows:
        people = [x.name for x in t.tags if x.kind == "person"] or ["(unknown)"]
        for p in people:
            out[p] += OWED_SIGN[t.direction] * t.amount_base_minor // len(people)
    return {k: v for k, v in out.items() if v != 0}


async def owes(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    b = balances(t.s, t.user.id)
    if not b:
        return [
            OutboundMessage(text='🤝 All square, nobody owes anybody. Log with "lent 20 to arjun".')
        ]
    theirs = sorted(((p, v) for p, v in b.items() if v > 0), key=lambda kv: -kv[1])
    mine = sorted(((p, -v) for p, v in b.items() if v < 0), key=lambda kv: -kv[1])
    lines = []
    if theirs:
        lines.append("Owed to you:")
        lines += [f"• {p.capitalize()}: {money.fmt(v, t.base)}" for p, v in theirs]
    if mine:
        lines.append("You owe:")
        lines += [f"• {p.capitalize()}: {money.fmt(v, t.base)}" for p, v in mine]
    net = sum(b.values())
    lines.append(f"Net: {money.fmt(net, t.base, sign=True)}")
    return [OutboundMessage(text="\n".join(lines))]


def install(services: Services) -> None:
    services.orchestrator.extra_commands["lending"] = owes
