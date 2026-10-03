import asyncio

import pytest

from pocket.llm.backends.fake import BrokenBackend, ChaosBackend, FakeBackend
from pocket.llm.quota import QuotaTracker
from pocket.llm.router import (
    CostCapExceeded,
    LLMRouter,
    LLMUnavailable,
    PromptBundle,
    RateLimited,
    RouterConfig,
    SchemaParseError,
    parse_structured,
)
from pocket.llm.schemas import ExtractionResult, Intent, IntentResult
from tests.support.stub_llm import StubLLM

CFG = RouterConfig.from_dict(
    {
        "router": {
            "fast": {"primary": "a/one", "fallbacks": ["b/two", "stub/v1"]},
            "strict": {"primary": "a/one", "fallbacks": ["b/two"]},
        },
        "purposes": {"intent": "fast", "extract": "strict"},
    }
)

PROMPT = PromptBundle(messages=[{"role": "user", "content": "hi"}], ctx={"text": "23 eur coffee"})


def make(a, b, **kw):
    calls = []
    router = LLMRouter(CFG, {"a": a, "b": b, "stub": StubLLM()}, call_sink=calls.append, **kw)
    return router, calls


async def test_primary_success():
    a = FakeBackend()
    a.queue("intent", {"intent": "ADD", "confidence": 0.9})
    router, calls = make(a, FakeBackend())
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.value.intent is Intent.ADD
    assert res.model == "a/one"
    assert len(calls) == 1 and calls[0]["success"]


async def test_falls_back_on_server_error():
    b = FakeBackend()
    b.queue("intent", {"intent": "QUERY", "confidence": 0.8})
    router, calls = make(BrokenBackend(), b)
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "b/two"
    assert [c["success"] for c in calls] == [False, True]
    assert calls[0]["error"].startswith("server_error")


async def test_schema_failure_retries_same_model_once():
    a = FakeBackend()
    a.queue("intent", '{"intent": "NOPE"}', {"intent": "ADD", "confidence": 0.7})
    router, calls = make(a, FakeBackend())
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "a/one" and res.attempts == 2


async def test_schema_failure_twice_moves_on():
    a = FakeBackend()
    a.queue("intent", "garbage", '{"broken": ')
    b = FakeBackend()
    b.queue("intent", {"intent": "HELP", "confidence": 0.99})
    router, calls = make(a, b)
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "b/two"
    assert len(calls) == 3


async def test_rate_limit_does_not_retry_and_backs_off():
    a = FakeBackend()
    a.queue("intent", RateLimited("429"))
    b = FakeBackend()
    b.queue("intent", {"intent": "ADD", "confidence": 0.9}, {"intent": "ADD", "confidence": 0.9})
    router, _ = make(a, b)
    await router.structured("intent", PROMPT, IntentResult)
    assert router.quota.near_limit("a/one")
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "b/two"
    assert len(a.calls) == 1


async def test_all_fail_raises():
    router, _ = make(BrokenBackend(), BrokenBackend())
    with pytest.raises(LLMUnavailable) as e:
        await router.structured("extract", PROMPT, ExtractionResult)
    assert len(e.value.errors) == 2


async def test_last_model_in_chain_answers():
    router, _ = make(BrokenBackend(), BrokenBackend())
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "stub/v1"
    assert res.value.intent is Intent.ADD


async def test_unavailable_models_skipped():
    a = FakeBackend()
    a.enabled = False
    b = FakeBackend()
    b.queue("intent", {"intent": "ADD", "confidence": 0.9})
    router, calls = make(a, b)
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "b/two" and len(calls) == 1


async def test_quota_skip():
    a = FakeBackend()
    b = FakeBackend()
    b.queue("intent", {"intent": "ADD", "confidence": 0.9})
    router, _ = make(a, b, quota=QuotaTracker({"a/one": {"rpm": 2}}))
    router.quota.record("a/one")
    router.quota.record("a/one")
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "b/two" and not a.calls


async def test_cost_cap_only_uses_free_backends():
    a = FakeBackend()
    a.free = False
    b = FakeBackend()
    b.free = False
    router, _ = make(a, b, cost_guard=lambda: True)
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "stub/v1"
    with pytest.raises(CostCapExceeded):
        await router.structured("extract", PROMPT, ExtractionResult)


async def test_timeout_falls_through():
    class Slow(FakeBackend):
        async def complete(self, *a, **k):
            await asyncio.sleep(5)

    b = FakeBackend()
    b.queue("intent", {"intent": "ADD", "confidence": 0.9})
    router, calls = make(Slow(), b, timeout=0.05)
    res = await router.structured("intent", PROMPT, IntentResult)
    assert res.model == "b/two"
    assert calls[0]["error"].startswith("timeout")


async def test_chaos_eventually_lands():
    inner = FakeBackend(responder=lambda p, pr: {"intent": "ADD", "confidence": 0.9})
    chaos = ChaosBackend(inner, p_error=0.3, p_malformed=0.3, p_timeout=0, p_rate_limit=0.2, seed=7)
    router, _ = make(chaos, chaos)
    for _ in range(20):
        res = await router.structured("intent", PROMPT, IntentResult)
        assert res.value.intent is Intent.ADD  # stub or fake, never a crash


def test_parse_structured_handles_fences_and_chatter():
    assert (
        parse_structured('```json\n{"intent":"ADD","confidence":1}\n```', IntentResult).intent
        == "ADD"
    )
    assert parse_structured(
        'Sure! {"intent":"HELP","confidence":0.5} hope that helps', IntentResult
    )
    with pytest.raises(SchemaParseError):
        parse_structured("no json here", IntentResult)


def test_provider_order_flips_every_chain():
    cfg = RouterConfig.from_dict(
        {
            "router": {
                "x": {"primary": "openai/a", "fallbacks": ["gemini/b", "gemini/c", "xai/d"]},
                "y": {"primary": "gemini/c", "fallbacks": ["openai/a"]},
            }
        }
    )
    flipped = cfg.reordered("gemini,openai")
    assert flipped.chains["x"].models == ["gemini/b", "gemini/c", "openai/a", "xai/d"]
    assert flipped.chains["y"].models == ["gemini/c", "openai/a"]
    assert cfg.reordered("openai").chains["y"].models == ["openai/a", "gemini/c"]
