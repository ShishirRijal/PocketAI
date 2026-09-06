"""Transaction directions. Lending ones (§12.13) are tracked per person via a
`person` tag and never count as spending or income."""

from __future__ import annotations

DIRECTIONS = ("expense", "income", "transfer", "lent", "borrowed", "got_back", "paid_back")
LENDING = frozenset({"lent", "borrowed", "got_back", "paid_back"})

# effect on "how much this person owes me"
OWED_SIGN = {"lent": 1, "paid_back": 1, "borrowed": -1, "got_back": -1}

VERB = {
    "lent": "Lent",
    "borrowed": "Borrowed",
    "got_back": "Got back",
    "paid_back": "Paid back",
}
PREP = {"lent": "to", "borrowed": "from", "got_back": "from", "paid_back": "to"}
