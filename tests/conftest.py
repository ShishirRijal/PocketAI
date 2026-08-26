import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

CATEGORY_NAMES = [
    "Groceries",
    "Cafes",
    "Restaurants",
    "Transport",
    "Rent",
    "Utilities",
    "Subscriptions",
    "Shopping",
    "Health",
    "Entertainment",
    "Travel",
    "Education",
    "Gifts",
    "Salary",
    "Miscellaneous",
]


@pytest.fixture
def rules_ctx():
    return {
        "today": "2026-09-28",
        "base_currency": "EUR",
        "timezone": "Europe/Tallinn",
        "categories": [{"id": i + 1, "name": n} for i, n in enumerate(CATEGORY_NAMES)],
        "recent": [],
    }


def golden_messages():
    return [
        json.loads(line)
        for line in (FIXTURES / "messages.jsonl").read_text().splitlines()
        if line.strip()
    ]
