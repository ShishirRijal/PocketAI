async def test_lending_flow(say):
    r = await say("lent 20 to arjun")
    assert "🤝 Lent €20.00 to Arjun" in r
    await say("borrowed 50 from sita")
    await say("arjun paid me back 15")
    r = await say("owes")
    assert "Owed to you:\n• Arjun: €5.00" in r
    assert "You owe:\n• Sita: €50.00" in r
    assert "Net: -€45.00" in r


async def test_lending_not_spending(say):
    await say("lent 20 to arjun")
    await say("coffee 3")
    r = await say("how much this month?")
    assert "€3.00 spent across 1 transaction" in r


async def test_all_square(say):
    await say("lent 20 to maria")
    await say("maria paid me back 20")
    assert "All square" in await say("owes")


async def test_person_profile(say):
    await say("coffee w/ arjun 6.50")
    await say("dinner 30 with arjun")
    await say("lent 20 to arjun")
    r = await say("person arjun")
    assert "👤 Arjun" in r and "across 2" in r
    assert "Owes you €20.00" in r
    assert "don't know" in await say("person zed")
