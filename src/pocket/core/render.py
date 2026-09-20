"""How Pocket talks. All user-facing strings live here so the tone stays consistent."""

from __future__ import annotations

from datetime import datetime

from pocket.core import money
from pocket.core.dates import humanize_when
from pocket.core.directions import LENDING, PREP, VERB
from pocket.core.policy import Proposal
from pocket.data.models import Transaction


def amount_str(amount_minor: int, currency: str, base_minor: int, base: str) -> str:
    s = money.fmt(amount_minor, currency)
    if currency != base:
        s += f" (~{money.fmt(base_minor, base)})"
    return s


def signed(direction: str, s: str) -> str:
    return f"+{s}" if direction == "income" else s


def tags_str(names: list[str]) -> str:
    return " ".join(f"#{n}" for n in names)


def _lending_head(direction: str, amount: str, people: list[str]) -> str:
    who = people[0].capitalize() if people else "someone"
    return f"🤝 {VERB[direction]} {amount} {PREP[direction]} {who}"


def txn_line(t: Transaction, base: str, tz: str, *, with_when: bool = True) -> str:
    amount = amount_str(t.amount_minor, t.currency, t.amount_base_minor, base)
    if t.direction in LENDING:
        people = [x.name for x in t.tags if x.kind == "person"]
        bits = [_lending_head(t.direction, amount, people)]
        if with_when:
            bits.append(humanize_when(t.occurred_at, tz))
        return " · ".join(bits)
    bits = [signed(t.direction, amount)]
    bits.append(t.category.full_name if t.category else "Uncategorized")
    if t.merchant and t.merchant.lower() not in {x.name for x in t.tags}:
        bits.append(t.merchant)
    tg = tags_str([x.name for x in t.tags])
    if tg:
        bits.append(tg)
    if with_when:
        bits.append(humanize_when(t.occurred_at, tz))
    return " · ".join(bits)


def proposal_line(p: Proposal, base: str, tz: str) -> str:
    amount = amount_str(p.amount_minor, p.currency, p.amount_base_minor, base)
    if p.direction in LENDING:
        people = [n for n, k in p.tags if k == "person"]
        when = humanize_when(datetime.fromisoformat(p.occurred_at), tz)
        return f"{_lending_head(p.direction, amount, people)} · {when}"
    bits = [signed(p.direction, amount)]
    if p.new_category:
        bits.append(f"{p.new_category} (new)")
    else:
        bits.append(p.category_name or "Uncategorized")
    tg = tags_str([n for n, _ in p.tags])
    if tg:
        bits.append(tg)
    if p.note:
        bits.append(p.note)
    bits.append(humanize_when(datetime.fromisoformat(p.occurred_at), tz))
    return " · ".join(bits)


def fx_footer(proposals: list[Proposal], base: str) -> str | None:
    foreign = [p for p in proposals if p.currency != base and p.fx_rate]
    if not foreign:
        return None
    p = foreign[0]
    per_base = 1 / float(p.fx_rate)  # type: ignore[arg-type]
    src = f", {p.fx_source}" if p.fx_source else ""
    stale = " ⚠️ offline estimate" if p.fx_source == "static" else ""
    return f"Base currency {base}; {p.currency} converted at {per_base:.2f}{src}.{stale}"


UNDO_HINT = 'reply "undo" or "edit" within 5 min to change'

HELP = """Pocket — just tell me what you spent.

*Log*
• 23 eur groceries at rimi today
• coffee 4.5 and metro 2
• chiya 30 rs aja · salary 2500
• send a receipt photo or a voice note

*Fix*
• sorry it was 29 · make that groceries
• undo (u) — last action, 5 min
• edit (e) — recent list · edit 2 amount 29
• delete 3 (d 3)

*Ask*
• how much grocery this month?
• top merchants last month
• show today · show week · show month

*More*
• categories · category add Pets · category rename X to Y
• budget cafes 80 · budgets
• every 15th 12.99 spotify · recurring
• lent 20 to arjun · owes · person arjun
• split 60 dinner with arjun and sita
• export csv month · cost · digest · monthly · review
• currency eur · tz Europe/Lisbon"""


def small_talk(text: str) -> str | None:
    """Cheap canned replies for the obvious ones, before spending an LLM call."""
    import re

    t = text.strip().lower().rstrip("!.🙏 ")
    if re.fullmatch(
        r"(thanks|thank you|thx|ty|dhanyabad|dhanyavaad|cheers|great|nice|cool|perfect)( pocket)?",
        t,
    ):
        return "👍 Anytime."
    if re.fullmatch(
        r"(hi|hey|hello|yo|namaste|namaskar|good (morning|evening|afternoon))( pocket)?", t
    ):
        return 'Hey! 👋 Tell me what you spent ("12 lunch at wolt") or ask "how much this week?".'
    return None


CHITCHAT = (
    'I only keep your money log 🙂 Tell me something like "12 eur lunch" '
    'or ask "how much this week?". Send "help" for everything I can do.'
)

NO_AMOUNT = 'I couldn\'t find an amount in that. Try something like "12.50 lunch at wolt".'

LLM_DOWN = (
    "⏳ My parsers are having a moment. I saved your message and will process it "
    "shortly, no need to resend."
)

COST_CAP = "💸 Hit today's AI budget. Commands (undo, edit, show, help) still work; parsing resumes tomorrow."

RATE_LIMITED = "Easy there 🙂 too many messages in a minute, give me a few seconds."
