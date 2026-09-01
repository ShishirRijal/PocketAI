"""End-to-end conversations from §2 of the design doc, against the offline parser."""

from sqlalchemy import select

from pocket.data.models import Category, Transaction, TransactionVersion


def txns(services, include_deleted=False):
    with services.db.session() as s:
        q = select(Transaction).order_by(Transaction.id)
        rows = s.scalars(q).all()
        return [r for r in rows if include_deleted or r.deleted_at is None]


async def test_happy_path(say, services):
    r = await say("23 eur groceries at rimi today")
    assert r.startswith("✅ Logged €23.00 · Groceries")
    assert "#rimi" in r and "Today 21:14" in r
    [t] = txns(services)
    assert t.amount_minor == 2300 and t.currency == "EUR" and t.amount_base_minor == 2300
    assert t.category.name == "Groceries" and t.merchant == "Rimi"


async def test_correction(say, services):
    await say("23 eur groceries at rimi today")
    r = await say("sorry it was 29")
    assert "✏️ Updated last transaction: €23.00 → €29.00" in r
    [t] = txns(services)
    assert t.amount_minor == 2900
    with services.db.session() as s:
        v = s.scalars(select(TransactionVersion)).all()
        assert v[-1].diff["amount_minor"] == [2300, 2900]


async def test_two_transactions_confirm_yes(say, services):
    r = await say("coffee w/ arjun 6.50 and then metro 2")
    assert r.startswith("Two transactions?")
    assert "1. €6.50 · Cafes" in r and "2. €2.00 · Transport" in r
    assert not txns(services)
    r = await say("yes")
    assert "✅ Saved 2 transactions (€8.50 total)" in r
    assert len(txns(services)) == 2


async def test_two_transactions_pick_one(say, services):
    await say("coffee w/ arjun 6.50 and then metro 2")
    r = await say("2")
    assert "✅ Logged €2.00 · Transport" in r
    [t] = txns(services)
    assert t.amount_minor == 200


async def test_multi_then_correction(say, services):
    await say("coffee 6.50 and metro 2")
    r = await say("the metro was 2.40")
    assert "€2.40" in r and "Two transactions?" in r
    r = await say("yes")
    assert "€8.90 total" in r


async def test_novel_category_create(say, services, fake):
    # make the categorizer propose a new one
    services.router.config.chains["categorize"].primary = "fake/x"
    fake.queue(
        "categorize",
        {"new_category_name": "Documents/Admin", "confidence": 0.8, "rationale": "passport"},
    )
    r = await say("40 eur passport photos and printouts")
    assert "I don't have a category that fits" in r and 'Create "Documents/Admin"' in r
    r = await say("a")
    assert "✅ Logged €40.00 · Documents/Admin (new)" in r
    with services.db.session() as s:
        admin = s.scalars(select(Category).where(Category.name == "Admin")).one()
        assert admin.parent.name == "Documents"


async def test_novel_category_misc(say, services, fake):
    services.router.config.chains["categorize"].primary = "fake/x"
    fake.queue("categorize", {"new_category_name": "Documents", "confidence": 0.8})
    await say("40 eur passport photos")
    r = await say("b")
    assert "Miscellaneous" in r
    assert txns(services)[0].category.name == "Miscellaneous"


async def test_novel_category_user_names_existing(say, services, fake):
    services.router.config.chains["categorize"].primary = "fake/x"
    fake.queue("categorize", {"new_category_name": "Documents", "confidence": 0.8})
    await say("40 eur passport photos")
    r = await say("shopping")
    assert "Shopping" in r and "(new)" not in r


async def test_query(say, services):
    await say("23 eur groceries at rimi today")
    await say("12 eur groceries at selver")
    await say("coffee 4")
    r = await say("how much grocery this month?")
    assert r.startswith("September (so far) · Groceries: €35.00 spent across 2 transactions.")
    assert "Top merchants: Rimi €23.00, Selver €12.00." in r


async def test_multi_currency(say, services):
    r = await say("chiya 30 rupees aja")
    assert "₨30.00 (~€0.17) · Cafes" in r
    assert "NPR converted at 174.40" in r
    [t] = txns(services)
    assert t.currency == "NPR" and t.amount_minor == 3000 and t.amount_base_minor == 17


async def test_undo_add(say, services, clock):
    await say("lunch 12")
    r = await say("undo")
    assert "Undone" in r
    assert not txns(services)
    assert len(txns(services, include_deleted=True)) == 1


async def test_undo_window(say, services, clock):
    await say("lunch 12")
    clock.advance(minutes=6)
    r = await say("u")
    assert "only works for 5 min" in r
    assert len(txns(services)) == 1


async def test_undo_edit_restores(say, services):
    await say("23 eur groceries at rimi")
    await say("sorry it was 29")
    r = await say("undo")
    assert "Reverted" in r and "€23.00" in r
    assert txns(services)[0].amount_minor == 2300


async def test_delete_and_undo(say, services):
    await say("lunch 12")
    await say("coffee 3")
    r = await say("delete the coffee one")
    assert "🗑️ Deleted" in r and "€3.00" in r
    assert [t.amount_minor for t in txns(services)] == [1200]
    r = await say("undo")
    assert "Restored" in r
    assert len(txns(services)) == 2


async def test_edit_command_direct(say, services):
    await say("lunch 12")
    await say("coffee 3")
    r = await say("edit")
    assert "1. €3.00" in r and "2. €12.00" in r
    r = await say("edit 2 amount 14")
    assert "€12.00 → €14.00" in r
    r = await say("edit 1 category groceries")
    assert "Cafes → Groceries" in r


async def test_delete_command(say, services):
    await say("lunch 12")
    await say("coffee 3")
    r = await say("delete 2")
    assert "€12.00" in r
    assert [t.amount_minor for t in txns(services)] == [300]


async def test_duplicate_detection(say, services, clock):
    await say("23 eur groceries at rimi")
    clock.advance(minutes=1)
    r = await say("23 eur groceries at rimi")
    assert "Looks like a repeat" in r
    r = await say("no")
    assert "Dropped" in r
    assert len(txns(services)) == 1
    await say("23 eur groceries at rimi")
    await say("yes")
    assert len(txns(services)) == 2


async def test_low_confidence_asks(say, services):
    r = await say("12")
    assert "Save it?" in r
    assert not txns(services)
    r = await say("yes")
    assert "✅ Logged €12.00" in r


async def test_pending_ignored_when_new_message(say, services):
    await say("coffee 6.50 and metro 2")
    r = await say("how much this week?")
    assert "This week" in r
    # the pending proposal was dropped, not saved
    assert not txns(services)


async def test_help_and_chitchat(say):
    assert "Pocket" in await say("help")
    assert "money log" in await say("good morning")


async def test_show_today(say):
    await say("lunch 12")
    r = await say("show today")
    assert r.startswith("Today (so far): €12.00 across 1")


async def test_income(say, services):
    r = await say("got salary 2500")
    assert "+€2,500.00 · Salary" in r
    assert txns(services)[0].direction == "income"


async def test_learned_merchant_category(say, services):
    await say("12 at wolt")
    await say("edit 1 category restaurants")
    r = await say("15.5 at wolt")
    assert "Restaurants" in r


async def test_categories_and_rename(say):
    await say("lunch 12")
    r = await say("categories")
    assert "Restaurants — €12.00" in r
    r = await say("category rename restaurants to Eating Out")
    assert "Renamed Restaurants → Eating Out" in r
    r = await say("category add Pets")
    assert "Pets" in r


async def test_history(say):
    await say("lunch 12")
    await say("sorry it was 14")
    r = await say("history 1")
    assert "amount_minor" in r
