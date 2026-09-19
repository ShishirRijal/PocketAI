import sqlite3

from sqlalchemy import select

from pocket.data.models import Budget, Transaction
from pocket.services.import_v1 import import_v1

V1_SCHEMA = """
CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL NOT NULL, category TEXT NOT NULL DEFAULT 'Other',
  merchant TEXT, note TEXT, date TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), raw_message TEXT,
  source TEXT DEFAULT 'text', items TEXT NOT NULL DEFAULT '[]');
CREATE TABLE budgets (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL UNIQUE, monthly_limit REAL NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE lendings (id INTEGER PRIMARY KEY AUTOINCREMENT, person_name TEXT NOT NULL, amount REAL NOT NULL,
  amount_returned REAL NOT NULL DEFAULT 0, is_settled INTEGER NOT NULL DEFAULT 0, due_date TEXT, note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')), settled_at TEXT);
"""


def make_v1(path):
    c = sqlite3.connect(path)
    c.executescript(V1_SCHEMA)
    c.execute(
        "insert into expenses (amount, category, merchant, date, raw_message, items) values (450, 'Food', 'Bhojan Griha', '2026-05-09', 'khana 450', '[\"dal bhat\"]')"
    )
    c.execute(
        "insert into expenses (amount, category, date, raw_message) values (120, 'Other', '2026-05-10', 'misc 120')"
    )
    c.execute("insert into expenses (amount, category, date) values (80, 'Pets', '2026-05-10')")
    c.execute("insert into budgets (category, monthly_limit) values ('Food', 15000)")
    c.execute(
        "insert into lendings (person_name, amount, amount_returned, note) values ('Arjun', 1000, 400, 'bike repair')"
    )
    c.commit()
    c.close()


async def test_import_v1(services, tmp_path, say):
    v1 = tmp_path / "v1.db"
    make_v1(v1)
    dry = import_v1(services.db, services.user_id, v1, "NPR")
    assert dry.expenses == 3 and dry.budgets == 1 and dry.lendings == 2
    assert "Pets" in dry.new_categories
    with services.db.session() as s:
        assert s.scalars(select(Transaction)).all() == []  # dry run wrote nothing

    rep = import_v1(services.db, services.user_id, v1, "NPR", apply=True)
    assert "Imported: 3 expenses (₨650.00)" in rep.text(True)
    with services.db.session() as s:
        rows = s.scalars(select(Transaction).order_by(Transaction.id)).all()
        food = rows[0]
        assert (
            food.currency == "NPR"
            and food.amount_minor == 45000
            and food.category.name == "Restaurants"
        )
        assert food.amount_base_minor < food.amount_minor  # converted to EUR
        assert "dal bhat" in food.note
        assert s.scalars(select(Budget)).one().category.name == "Restaurants"
    assert "Arjun" in await say("owes")

    again = import_v1(services.db, services.user_id, v1, "NPR", apply=True)
    assert again.expenses == 0 and again.skipped == 5


def test_idempotent_insert_race(services):
    from pocket.data.db import utcnow
    from pocket.data.repositories import RawMessageRepo

    with services.db.session() as s:
        RawMessageRepo(s).insert_idempotent(
            user_id=services.user_id,
            channel="whatsapp",
            channel_msg_id="SMrace",
            text="a",
            media_json=None,
            received_at=utcnow(),
        )
    with services.db.session() as s:
        repo = RawMessageRepo(s)
        real = repo.by_channel_id
        calls = []

        def stale_lookup(channel, msg_id):  # the other worker hadn't committed when we looked
            calls.append(1)
            return None if len(calls) == 1 else real(channel, msg_id)

        repo.by_channel_id = stale_lookup
        row, created = repo.insert_idempotent(
            user_id=services.user_id,
            channel="whatsapp",
            channel_msg_id="SMrace",
            text="a",
            media_json=None,
            received_at=utcnow(),
        )
        assert created is False and row.text == "a"
