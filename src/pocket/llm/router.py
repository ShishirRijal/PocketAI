"""Per-purpose model chains with fallback.

Each purpose (intent, extract, ...) has a primary model and fallbacks. We walk the
chain ourselves instead of using litellm's Router because the failover rules are
specific:

- rate limit / timeout / 5xx / context length  -> next model
- schema validation failure                    -> retry same model once, then next
- model within 5% of its free-tier quota        -> skipped before calling
- daily cost cap hit                            -> only zero-cost backends (rules/local)

Every attempt is reported to `call_sink` so it lands in the llm_calls table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from pocket.llm.quota import QuotaTracker

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------- errors


class LLMError(Exception):
    kind = "other"


class RateLimited(LLMError):
    kind = "rate_limit"


class Timeout(LLMError):
    kind = "timeout"


class ServerError(LLMError):
    kind = "server_error"


class ContextTooLong(LLMError):
    kind = "context_length"


class SchemaParseError(LLMError):
    kind = "schema_parse_fail"


class AuthError(LLMError):
    kind = "auth"


class LLMUnavailable(Exception):
    """Every model in the chain failed."""

    def __init__(self, purpose: str, errors: list[tuple[str, str]]):
        self.purpose = purpose
        self.errors = errors
        super().__init__(f"all models failed for {purpose}: {errors}")


class CostCapExceeded(Exception):
    pass


# ---------------------------------------------------------------- backend protocol


@dataclass
class PromptBundle:
    """What a stage hands the router.

    `messages` is for real models. `ctx` is the same information as plain data,
    for the offline rules backend (and the fake backend in tests).
    """

    messages: list[dict[str, Any]]
    ctx: dict[str, Any] = field(default_factory=dict)
    images: list[str] = field(default_factory=list)  # data: urls or http urls


@dataclass
class BackendResponse:
    content: str | dict[str, Any]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float = 0.0


class Backend(Protocol):
    free: bool

    def available(self, model: str) -> bool: ...

    async def complete(
        self,
        model: str,
        purpose: str,
        prompt: PromptBundle,
        schema: type[BaseModel],
        timeout: float,
    ) -> BackendResponse: ...


# ---------------------------------------------------------------- config


@dataclass
class Chain:
    primary: str
    fallbacks: list[str] = field(default_factory=list)

    @property
    def models(self) -> list[str]:
        return [self.primary, *self.fallbacks]


@dataclass
class RouterConfig:
    chains: dict[str, Chain]
    quotas: dict[str, dict[str, int]] = field(default_factory=dict)
    # purposes that alias other chains, e.g. intent -> fast
    aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> RouterConfig:
        raw = yaml.safe_load(path.read_text())
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RouterConfig:
        chains = {
            name: Chain(primary=c["primary"], fallbacks=list(c.get("fallbacks") or []))
            for name, c in (raw.get("router") or {}).items()
        }
        return cls(
            chains=chains,
            quotas=raw.get("quotas") or {},
            aliases=raw.get("purposes") or {},
        )

    def chain_for(self, purpose: str) -> Chain:
        name = self.aliases.get(purpose, purpose)
        if name not in self.chains:
            raise KeyError(f"no llm chain configured for purpose {purpose!r}")
        return self.chains[name]


# ---------------------------------------------------------------- parsing

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_structured[T: BaseModel](content: str | dict[str, Any], schema: type[T]) -> T:
    if isinstance(content, dict):
        data = content
    else:
        text = _FENCE.sub("", content.strip())
        # some models chat before the json; grab the outermost object
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1:
                raise SchemaParseError(f"no json object in response: {content[:200]!r}")
            text = text[start : end + 1]
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise SchemaParseError(f"invalid json: {e}") from e
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        raise SchemaParseError(f"schema mismatch: {e.errors()[:3]}") from e


# ---------------------------------------------------------------- router


@dataclass
class CallResult[T]:
    value: T
    model: str
    attempts: int
    cost_usd: float
    latency_ms: int


CallSink = Callable[[dict[str, Any]], None]


class LLMRouter:
    def __init__(
        self,
        config: RouterConfig,
        backends: dict[str, Backend],
        default_backend: Backend | None = None,
        *,
        quota: QuotaTracker | None = None,
        call_sink: CallSink | None = None,
        cost_guard: Callable[[], bool] | None = None,
        timeout: float = 10.0,
    ):
        """
        backends: model prefix -> backend, e.g. {"rules": RulesBackend(), "fake": fake}.
        Anything without a matching prefix goes to `default_backend` (litellm).
        cost_guard: returns True when the daily cap is hit.
        """
        self.config = config
        self.backends = backends
        self.default_backend = default_backend
        self.quota = quota or QuotaTracker(config.quotas)
        self.call_sink = call_sink
        self.cost_guard = cost_guard
        self.timeout = timeout

    def backend_for(self, model: str) -> Backend | None:
        prefix = model.split("/", 1)[0]
        return self.backends.get(prefix, self.default_backend)

    def usable_models(self, purpose: str) -> list[str]:
        out = []
        for m in self.config.chain_for(purpose).models:
            b = self.backend_for(m)
            if b is not None and b.available(m):
                out.append(m)
        return out

    async def structured(
        self,
        purpose: str,
        prompt: PromptBundle,
        schema: type[T],
        *,
        raw_message_id: int | None = None,
    ) -> CallResult[T]:
        chain = self.config.chain_for(purpose).models
        capped = bool(self.cost_guard and self.cost_guard())
        errors: list[tuple[str, str]] = []
        attempts = 0
        total_cost = 0.0
        started = time.perf_counter()

        for model in chain:
            backend = self.backend_for(model)
            if backend is None or not backend.available(model):
                errors.append((model, "unavailable"))
                continue
            if capped and not backend.free:
                errors.append((model, "cost_cap"))
                continue
            if self.quota.near_limit(model):
                errors.append((model, "quota"))
                log.info("skipping %s for %s: near quota", model, purpose)
                continue

            for try_no in range(2):  # second try only for schema failures
                attempts += 1
                t0 = time.perf_counter()
                resp: BackendResponse | None = None
                err: LLMError | None = None
                value: T | None = None
                try:
                    self.quota.record(model)
                    resp = await asyncio.wait_for(
                        backend.complete(model, purpose, prompt, schema, self.timeout),
                        timeout=self.timeout + 1,
                    )
                    value = parse_structured(resp.content, schema)
                except TimeoutError:
                    err = Timeout(f"timed out after {self.timeout}s")
                except LLMError as e:
                    err = e
                except Exception as e:  # unknown backend blowups count as server errors
                    log.exception("backend %s crashed", model)
                    err = ServerError(repr(e))
                latency = int((time.perf_counter() - t0) * 1000)
                cost = resp.cost_usd if resp else 0.0
                total_cost += cost
                self._sink(
                    purpose=purpose,
                    model=model,
                    raw_message_id=raw_message_id,
                    prompt=prompt,
                    resp=resp,
                    latency_ms=latency,
                    cost=cost,
                    error=err,
                )
                if err is None and value is not None:
                    return CallResult(
                        value=value,
                        model=model,
                        attempts=attempts,
                        cost_usd=total_cost,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                assert err is not None
                errors.append((model, f"{err.kind}: {err}"))
                if isinstance(err, RateLimited):
                    self.quota.mark_exhausted(model)
                if not isinstance(err, SchemaParseError) or try_no == 1:
                    break
                log.info("schema parse failed on %s (%s), retrying once", model, err)

        if capped and all(e[1] in ("cost_cap", "unavailable") for e in errors):
            raise CostCapExceeded()
        raise LLMUnavailable(purpose, errors)

    def _sink(
        self,
        *,
        purpose: str,
        model: str,
        raw_message_id: int | None,
        prompt: PromptBundle,
        resp: BackendResponse | None,
        latency_ms: int,
        cost: float,
        error: LLMError | None,
    ) -> None:
        if not self.call_sink:
            return
        try:
            self.call_sink(
                {
                    "raw_message_id": raw_message_id,
                    "purpose": purpose,
                    "model": model,
                    "prompt_tokens": resp.prompt_tokens if resp else None,
                    "completion_tokens": resp.completion_tokens if resp else None,
                    "cost_usd": cost,
                    "latency_ms": latency_ms,
                    "success": error is None,
                    "error": f"{error.kind}: {error}"[:2000] if error else None,
                    "request_json": {"messages": _redact_images(prompt.messages)},
                    "response_json": (
                        resp.content
                        if resp and isinstance(resp.content, dict)
                        else {"text": resp.content}
                        if resp
                        else None
                    ),
                }
            )
        except Exception:  # logging must never break the pipeline
            log.exception("failed to record llm call")


def _redact_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = [
                {"type": "image_url", "image_url": "<redacted>"}
                if isinstance(p, dict) and p.get("type") == "image_url"
                else p
                for p in c
            ]
            m = {**m, "content": c}
        out.append(m)
    return out
