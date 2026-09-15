"""Group splits: "split 60 dinner with arjun and sita".

Your share becomes the expense (in the right category); everyone else's share
becomes a `lent` to that person, so `owes` shows who still has to pay you back.
Remainder cents stay with you. Always confirmed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pocket.channels.base import OutboundMessage
from pocket.core import money
from pocket.core.policy import Proposal
from pocket.data.repositories import normalize_tag

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.wiring import Services

_WITH = re.compile(r"\s+(?:with|between(?:\s+me)?(?:\s+and)?|w/)\s+(.+)$", re.IGNORECASE)
_NOT_NAMES = {"me", "myself", "i", "everyone", "friends", "the", "team"}


def parse_split(text: str) -> tuple[str, list[str], int | None]:
    """-> (what text, people, explicit ways) e.g. ("60 dinner", ["arjun", "sita"], None)."""
    body = re.sub(r"^split\s+", "", text.strip(), flags=re.IGNORECASE)
    ways = None
    if m := re.search(r"\b(\d+)\s+ways?\b", body, re.IGNORECASE):
        ways = int(m.group(1))
        body = (body[: m.start()] + body[m.end() :]).strip()
    people: list[str] = []
    if m := _WITH.search(body):
        raw = re.split(r"\s*(?:,|&|\band\b|\+)\s*", m.group(1))
        people = [
            normalize_tag(p) for p in raw if p.strip() and p.strip().lower() not in _NOT_NAMES
        ]
        body = body[: m.start()].strip()
    return body, people, ways


async def split_cmd(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    what, people, ways = parse_split(cmd.args["text"])
    n = ways or (len(people) + 1)
    if n < 2:
        return [
            OutboundMessage(
                text='Split with who? e.g. "split 60 dinner with arjun and sita" or "split 45 taxi 3 ways".'
            )
        ]
    ext = await o.pipeline.extract(what, o._uctx(t))
    if not ext.transactions:
        return [OutboundMessage(text='What was the total? e.g. "split 60 dinner with arjun".')]
    base = await o.propose_from_extracted(t, ext.transactions[0], what)
    total = base.amount_minor
    share = total // n
    mine = total - share * (n - 1)  # leftover cents stay with me

    def scaled(p: Proposal, amount: int, **update) -> Proposal:
        ratio = amount / total if total else 0
        return p.model_copy(
            update={
                "amount_minor": amount,
                "amount_base_minor": round(p.amount_base_minor * ratio),
                "duplicate_of": [],
                **update,
            }
        )

    me = scaled(base, mine, note=f"my share of {money.fmt(total, base.currency)}")
    others = [
        scaled(
            base,
            share,
            direction="lent",
            category_id=None,
            category_name=None,
            new_category=None,
            merchant=None,
            note=f"share of {what}".strip(),
            tags=[(p, "person")],
        )
        for p in people
    ]
    names = ", ".join(p.capitalize() for p in people) or f"{n - 1} others"
    intro = f"➗ Split {money.fmt(total, base.currency)} {n} ways (you + {names}):"
    return await o.decide(t, [me, *others], force_confirm=True, intro=intro)


def install(services: Services) -> None:
    services.orchestrator.extra_commands["split"] = split_cmd
