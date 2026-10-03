import json
from pathlib import Path

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


def golden_messages():
    return [
        json.loads(line)
        for line in (FIXTURES / "messages.jsonl").read_text().splitlines()
        if line.strip()
    ]
