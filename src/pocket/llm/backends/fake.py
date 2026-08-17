"""Test doubles: a scripted backend and a chaos backend.

Chaos mode (§12.20): fails, returns malformed JSON, times out — exercises the
fallback and policy code paths without spending anything.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from pocket.llm.router import (
    BackendResponse,
    LLMError,
    PromptBundle,
    RateLimited,
    ServerError,
)

Responder = Callable[[str, PromptBundle], dict[str, Any] | str | BaseModel | Exception]


class FakeBackend:
    """Queue up responses per purpose, or give it a responder function."""

    free = True

    def __init__(self, responder: Responder | None = None, cost_usd: float = 0.0):
        self.responder = responder
        self.queued: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[tuple[str, str, PromptBundle]] = []
        self.cost_usd = cost_usd
        self.enabled = True

    def available(self, model: str) -> bool:
        return self.enabled

    def queue(self, purpose: str, *responses: Any) -> None:
        self.queued[purpose].extend(responses)

    async def complete(
        self,
        model: str,
        purpose: str,
        prompt: PromptBundle,
        schema: type[BaseModel],
        timeout: float,
    ) -> BackendResponse:
        self.calls.append((model, purpose, prompt))
        if self.queued[purpose]:
            out = self.queued[purpose].popleft()
        elif self.responder:
            out = self.responder(purpose, prompt)
        else:
            raise ServerError(f"fake backend has nothing queued for {purpose}")
        if isinstance(out, Exception):
            raise out
        if isinstance(out, BaseModel):
            out = out.model_dump(mode="json")
        return BackendResponse(
            content=out, prompt_tokens=10, completion_tokens=5, cost_usd=self.cost_usd
        )


class ChaosBackend:
    """Wraps another backend and randomly breaks it."""

    free = True

    def __init__(
        self,
        inner: Any,
        *,
        p_error: float = 0.2,
        p_malformed: float = 0.2,
        p_timeout: float = 0.1,
        p_rate_limit: float = 0.1,
        seed: int | None = None,
    ):
        self.inner = inner
        self.p = (p_error, p_malformed, p_timeout, p_rate_limit)
        self.rng = random.Random(seed)

    def available(self, model: str) -> bool:
        return True

    async def complete(
        self,
        model: str,
        purpose: str,
        prompt: PromptBundle,
        schema: type[BaseModel],
        timeout: float,
    ) -> BackendResponse:
        p_error, p_malformed, p_timeout, p_rate = self.p
        r = self.rng.random()
        if r < p_error:
            raise ServerError("chaos: 503")
        r -= p_error
        if r < p_rate:
            raise RateLimited("chaos: 429")
        r -= p_rate
        if r < p_timeout:
            await asyncio.sleep(timeout + 5)
        r -= p_timeout
        if r < p_malformed:
            return BackendResponse(content='{"transactions": [{"amount": "twenty', cost_usd=0.0)
        inner_model = model.split("/", 1)[1] if "/" in model else model
        return await self.inner.complete(inner_model, purpose, prompt, schema, timeout)


class BrokenBackend:
    """Always raises. For testing fallthrough."""

    free = True

    def __init__(self, error: LLMError | None = None):
        self.error = error or ServerError("down")
        self.calls = 0

    def available(self, model: str) -> bool:
        return True

    async def complete(self, *a: Any, **k: Any) -> BackendResponse:
        self.calls += 1
        raise self.error
