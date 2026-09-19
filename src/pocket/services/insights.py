"""Small, honest nudges after logging. Plain statistics, no LLM, no advice.

Currently: unusually large for its category ("≈4× your usual Cafes spend"),
judged against the median of the last 90 days, needing at least 6 data points.
"""

from __future__ import annotations

from datetime import timedelta
from statistics import median
from typing import TYPE_CHECKING

from sqlalchemy import select

from pocket.core import money
from pocket.data.models import Transaction

if TYPE_CHECKING:
    from pocket.core.orchestrator import Turn
    from pocket.wiring import Services

MIN_SAMPLES = 6
FACTOR = 3.0


def unusual(t: Turn, saved: list[Transaction]) -> str | None:
    lines = []
    for x in saved:
        if x.direction != "expense" or x.category_id is None:
            continue
        history = t.s.scalars(
            select(Transaction.amount_base_minor).where(
                Transaction.user_id == t.user.id,
                Transaction.category_id == x.category_id,
                Transaction.direction == "expense",
                Transaction.deleted_at.is_(None),
                Transaction.id != x.id,
                Transaction.occurred_at >= x.occurred_at - timedelta(days=90),
            )
        ).all()
        if len(history) < MIN_SAMPLES:
            continue
        typical = median(history)
        # ignore small absolute jumps: a €2 coffee vs a €7 one isn't news
        if (
            typical > 0
            and x.amount_base_minor >= FACTOR * typical
            and x.amount_base_minor - typical >= money.to_minor(15, t.base)
        ):
            ratio = x.amount_base_minor / typical
            name = x.category.name if x.category else "this category"
            lines.append(
                f"📈 That's about {ratio:.0f}× your usual {name} spend (typically {money.fmt(round(typical), t.base)})."
            )
    return "\n".join(lines) or None


def install(services: Services) -> None:
    services.orchestrator.after_commit.append(unusual)
