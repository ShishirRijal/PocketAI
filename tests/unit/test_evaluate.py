from pocket.config import Settings
from pocket.services.evaluate import GOLDEN, HOLDOUT, _load, evaluate, render_markdown


async def test_eval_rules_only():
    settings = Settings(_env_file=None)
    cases = _load(HOLDOUT)
    [score] = await evaluate(["rules/v1"], settings, cases)
    row = score.row()
    assert row["intent_%"] >= 90 and row["extract_%"] >= 90
    md = render_markdown([score], len(cases))
    assert "| `rules/v1` |" in md
    assert len(_load(GOLDEN)) > 30
