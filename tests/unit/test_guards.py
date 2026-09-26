from pocket.llm.guards import currency_guard
from pocket.llm.schemas import ExtractedTransaction, ExtractionResult


def _res(*pairs):
    return ExtractionResult(
        transactions=[ExtractedTransaction(amount=a, currency=c, confidence=0.9) for a, c in pairs]
    )


def test_symbol_beats_model():
    assert currency_guard("₹450 dosa", _res((450, "NPR"))).transactions[0].currency == "INR"
    assert currency_guard("£8 lunch in london", _res((8, "EUR"))).transactions[0].currency == "GBP"


def test_leaves_bare_numbers_alone():
    assert currency_guard("lunch 12", _res((12, "EUR"))).transactions[0].currency == "EUR"
    # model converted/unmatched amount: don't touch
    assert currency_guard("$10 book", _res((9.2, "EUR"))).transactions[0].currency == "EUR"


def test_multi():
    r = currency_guard("coffee 3 eur and chiya 30 rs", _res((3, "EUR"), (30, "EUR")))
    assert [t.currency for t in r.transactions] == ["EUR", "NPR"]


def test_amount_guard():
    from pocket.llm.guards import amount_guard

    ok = amount_guard("lunch 12.50", _res((12.5, "EUR")))
    assert ok.transactions[0].confidence == 0.9
    bad = amount_guard("lunch 12.50", _res((125, "EUR")))
    assert bad.transactions[0].confidence == 0.5
    assert amount_guard("dinner 1.2k rs", _res((1200, "NPR"))).transactions[0].confidence == 0.9
    assert amount_guard("twelve euro lunch", _res((12, "EUR"))).transactions[0].confidence == 0.9
