from pocket.config import Settings
from pocket.services.evaluate import GOLDEN, HOLDOUT, _load, evaluate, render_markdown
from tests.support.stub_llm import StubLLM


async def test_eval_scores_a_model():
    settings = Settings(_env_file=None)
    cases = _load(HOLDOUT)
    [score] = await evaluate(["stub/v1"], settings, cases, backends={"stub": StubLLM()})
    row = score.row()
    assert row["intent_%"] >= 90 and row["extract_%"] >= 90
    assert "| `stub/v1` |" in render_markdown([score], len(cases))
    assert len(_load(GOLDEN)) > 30
