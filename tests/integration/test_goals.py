from datetime import date

from pocket.services.goals import parse_deadline


def test_parse_deadline():
    today = date(2026, 9, 28)
    assert parse_deadline("march", today) == date(2027, 3, 31)
    assert parse_deadline("december 2026", today) == date(2026, 12, 31)
    assert parse_deadline("6 months", today) == date(2027, 3, 27)
    assert parse_deadline("2027-01-15", today) == date(2027, 1, 15)
    assert parse_deadline("whenever", today) is None


async def test_goal_flow(say):
    r = await say("goal japan 2000 by march")
    assert "🎯 Japan: €0.00 of €2,000.00" in r and "€285.72/month to make 2027-03-31" in r
    r = await say("save 500 japan")
    assert "💰 Saved €500.00 for Japan" in r and "25%" in r
    await say("save 1500 for the japan goal")
    assert "reached 🎉" in await say("goals")
    r = await say("how much this month?")
    assert "nothing spent" in r  # savings aren't spending
    assert "Took out" in await say("withdraw 100 from japan")
    assert "Closed goal Japan" in await say("goal done japan")
    assert "No goals" in await say("goals")


async def test_save_unknown_goal(say):
    assert "No goal called" in await say("save 50 for the car goal")
    r = await say("put 20 into groceries")
    assert "Logged €20.00 · Groceries" in r


async def test_undo_save(say):
    await say("goal bike 300")
    await say("save 100 bike")
    assert "Undone" in await say("undo")
    assert "€0.00 of €300.00" in await say("goals")
