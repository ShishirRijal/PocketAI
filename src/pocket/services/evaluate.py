"""Model bake-off: run the golden message set against one model at a time.

    pocket eval                                   # default candidates
    pocket eval --models gemini/gemini-flash-lite-latest,openai/gpt-4o-mini,rules/v1

For each model: intent accuracy, extraction accuracy (right number of
transactions with the right amounts/currencies/directions), category agreement,
p50/p95 latency and total cost. Uses tests/fixtures/messages.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pocket.config import PROJECT_ROOT, Settings
from pocket.core.categorize import CatRef
from pocket.llm.router import LLMRouter, LLMUnavailable, RouterConfig
from pocket.llm.stages import Pipeline, UserContext

GOLDEN = PROJECT_ROOT / "tests" / "fixtures" / "messages.jsonl"
# written after the rules parser was done and never tuned against: the fair comparison
HOLDOUT = PROJECT_ROOT / "tests" / "fixtures" / "holdout.jsonl"
CATEGORIES = [
    "Groceries", "Cafes", "Restaurants", "Transport", "Rent", "Utilities", "Subscriptions",
    "Shopping", "Health", "Entertainment", "Travel", "Education", "Gifts", "Salary", "Miscellaneous",
]  # fmt: skip
DEFAULT_MODELS = [
    "rules/v1",
    "gemini/gemini-flash-lite-latest",
    "gemini/gemini-3.8-flash",
    "openai/gpt-4o-mini",
]


@dataclass
class ModelScore:
    model: str
    intents: int = 0
    intents_ok: int = 0
    extracts: int = 0
    extracts_ok: int = 0
    cats: int = 0
    cats_ok: int = 0
    errors: int = 0
    latencies: list[float] = field(default_factory=list)
    cost: float = 0.0
    misses: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        pct = lambda a, b: round(100 * a / b) if b else None  # noqa: E731
        lat = sorted(self.latencies)
        return {
            "model": self.model,
            "intent_%": pct(self.intents_ok, self.intents),
            "extract_%": pct(self.extracts_ok, self.extracts),
            "category_%": pct(self.cats_ok, self.cats),
            "p50_ms": round(statistics.median(lat) * 1000) if lat else None,
            "p95_ms": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))] * 1000) if lat else None,
            "cost_usd": round(self.cost, 5),
            "errors": self.errors,
            "n": self.intents,
        }


def _ctx() -> UserContext:
    return UserContext(
        user_id=1,
        base_currency="EUR",
        timezone="Europe/Tallinn",
        now_local=datetime(2026, 9, 28, 21, 14, tzinfo=ZoneInfo("Europe/Tallinn")),
        categories=[CatRef(i + 1, n) for i, n in enumerate(CATEGORIES)],
    )


def _pipeline_for(model: str, settings: Settings, sink) -> Pipeline:
    from pocket.llm.backends.litellm_backend import LiteLLMBackend
    from pocket.llm.backends.rules import RulesBackend

    purposes = ["intent", "extract", "categorize", "edit", "delete", "query"]
    cfg = RouterConfig.from_dict({"router": {p: {"primary": model} for p in purposes}})
    router = LLMRouter(
        cfg,
        {"rules": RulesBackend()},
        LiteLLMBackend(),
        call_sink=sink,
        timeout=settings.llm_timeout_s * 2,
    )
    return Pipeline(router)


class _Pacer:
    """Keeps a model under its configured free-tier rpm and retries 429/503s,
    so the bake-off measures answers, not rate limits."""

    def __init__(self, model: str, settings: Settings, pipe: Pipeline, retries: int = 3):
        self.pipe = pipe
        quotas = RouterConfig.from_yaml(settings.llm_config_path).quotas
        rpm = (quotas.get(model) or {}).get("rpm")
        self.interval = 60 / rpm * 1.1 if rpm else 0.0
        self.retries = retries
        self.next_at = 0.0
        self.last_start = 0.0

    async def call(self, fn, *args):
        for attempt in range(self.retries + 1):
            wait = self.next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.next_at = time.monotonic() + self.interval
            self.last_start = time.perf_counter()
            try:
                return await fn(*args)
            except LLMUnavailable:
                if attempt == self.retries:
                    return None
                # the router benched the model after a 429; un-bench and back off
                self.pipe.router.quota._exhausted_until.clear()
                await asyncio.sleep(min(60, 8 * 2**attempt))
        return None


async def evaluate(
    models: list[str], settings: Settings, cases: list[dict] | None = None
) -> list[ModelScore]:
    cases = cases or [json.loads(x) for x in GOLDEN.read_text().splitlines() if x.strip()]
    scores = []
    for model in models:
        score = ModelScore(model)
        calls: list[dict] = []
        pipe = _pipeline_for(model, settings, calls.append)
        if not pipe.router.usable_models("intent"):
            score.errors = -1  # no credentials
            scores.append(score)
            continue
        u = _ctx()
        pacer = _Pacer(model, settings, pipe)
        for case in cases:
            intent = await pacer.call(pipe.intent, case["text"], u)
            if intent is None:
                # provider errors (429/503) aren't the model being wrong; keep them apart
                score.errors += 1
                continue
            score.intents += 1
            score.latencies.append(time.perf_counter() - pacer.last_start)
            if intent.intent.value == case["intent"]:
                score.intents_ok += 1
            else:
                score.misses.append(f"intent: {case['text']!r} -> {intent.intent.value}")
            if "txns" not in case:
                continue
            res = await pacer.call(pipe.extract, case["text"], u)
            if res is None:
                score.errors += 1
                continue
            score.extracts += 1
            score.latencies.append(time.perf_counter() - pacer.last_start)
            got = res.transactions
            ok = len(got) == len(case["txns"]) and all(
                abs(g.amount - w["amount"]) < 0.005
                and g.currency == w["currency"]
                and g.direction == w.get("direction", "expense")
                for g, w in zip(got, case["txns"], strict=False)
            )
            if ok:
                score.extracts_ok += 1
            else:
                score.misses.append(
                    f"extract: {case['text']!r} -> {[(g.amount, g.currency, g.direction) for g in got]}"
                )
            for g, w in zip(got, case["txns"], strict=False):
                if "category" in w:
                    score.cats += 1
                    if (g.category_hint or "").lower() == w["category"].lower():
                        score.cats_ok += 1
        score.cost = sum(float(c.get("cost_usd") or 0) for c in calls)
        scores.append(score)
    return scores


def render_markdown(scores: list[ModelScore], n_cases: int) -> str:
    rows = [s.row() for s in scores]
    head = (
        "| model | intent | extraction | category | p50 | p95 | cost | provider errors |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    lines = []
    for r in rows:
        if r["errors"] == -1:
            lines.append(f"| `{r['model']}` | no credentials | | | | | | |")
            continue
        fmt = lambda v, suf="%": "—" if v is None else f"{v}{suf}"  # noqa: E731
        lines.append(
            f"| `{r['model']}` | {fmt(r['intent_%'])} | {fmt(r['extract_%'])} | {fmt(r['category_%'])} "
            f"| {fmt(r['p50_ms'], ' ms')} | {fmt(r['p95_ms'], ' ms')} | ${r['cost_usd']:.4f} | {r['errors']} |"
        )
    return f"{n_cases} golden messages, {datetime.now():%Y-%m-%d}.\n\n{head}\n" + "\n".join(lines)


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def run_cli(models: list[str] | None, settings: Settings, out: Path | None) -> str:
    sections = []
    for title, path in (
        ("Held-out set (never tuned against)", HOLDOUT),
        ("Golden set (the rules parser was tuned on it)", GOLDEN),
    ):
        cases = _load(path)
        scores = asyncio.run(evaluate(models or DEFAULT_MODELS, settings, cases))
        misses = "\n".join(f"- `{s.model}` {m}" for s in scores for m in s.misses[:6])
        sections.append(
            f"### {title}\n\n{render_markdown(scores, len(cases))}"
            + (f"\n\n<details><summary>misses</summary>\n\n{misses}\n</details>" if misses else "")
        )
    report = "\n\n".join(sections)
    if out:
        out.write_text(report + "\n")
    return report
