"""The staged pipeline (§4.1). Each stage builds its prompt from the user's
context and asks the router for one structured result."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pocket.core.categorize import CatRef
from pocket.llm.cache import SystemPromptCache, build_messages, load_prompt, render
from pocket.llm.guards import amount_guard, currency_guard
from pocket.llm.router import LLMRouter, PromptBundle
from pocket.llm.schemas import (
    CategorizationResult,
    DeleteResolution,
    EditResolution,
    ExtractedTransaction,
    ExtractionResult,
    IntentResult,
    QueryPlan,
    ReceiptExtraction,
    Summary,
)


@dataclass
class UserContext:
    user_id: int
    base_currency: str
    timezone: str
    now_local: datetime
    categories: list[CatRef]  # most recently used first
    # [{"index": 1, "id": 312, "amount": "23.00", "currency": "EUR", "merchant": ...}]
    recent: list[dict[str, Any]] = field(default_factory=list)
    raw_message_id: int | None = None

    @property
    def today(self) -> str:
        return self.now_local.date().isoformat()

    def ctx_dict(self, text: str, **extra: Any) -> dict[str, Any]:
        """The same context as plain data, handed to backends alongside the
        messages (the scripted test backends read it; real models ignore it)."""
        return {
            "text": text,
            "today": self.today,
            "now": self.now_local.isoformat(timespec="minutes"),
            "timezone": self.timezone,
            "base_currency": self.base_currency,
            "categories": [{"id": c.id, "name": c.name} for c in self.categories],
            "recent": self.recent,
            **extra,
        }


def _recent_block(recent: list[dict[str, Any]]) -> str:
    if not recent:
        return "(nothing logged recently)"
    lines = []
    for r in recent:
        tags = " ".join(f"#{t}" for t in r.get("tags") or [])
        lines.append(
            f"{r['index']}. {r['amount']} {r['currency']} · {r.get('category') or '-'} · "
            f"{r.get('merchant') or '-'} · {r.get('when', '')} {tags}".rstrip()
        )
    return "\n".join(lines)


class Pipeline:
    def __init__(self, router: LLMRouter, prompt_cache: SystemPromptCache | None = None):
        self.router = router
        self.prompt_cache = prompt_cache or SystemPromptCache()

    def _system(self, u: UserContext) -> str:
        return self.prompt_cache.get(
            u.user_id, u.base_currency, u.timezone, [c.name for c in u.categories]
        )

    def _bundle(
        self, u: UserContext, stage: str, user_text: str, ctx: dict, **vars: Any
    ) -> PromptBundle:
        stage_text = render(load_prompt(stage), **vars) if vars else load_prompt(stage)
        return PromptBundle(
            messages=build_messages(self._system(u), stage_text, user_text), ctx=ctx
        )

    def _common(self, u: UserContext) -> dict[str, str]:
        return {
            "today": u.today,
            "weekday": u.now_local.strftime("%A"),
            "now": u.now_local.strftime("%Y-%m-%d %H:%M"),
        }

    async def intent(self, text: str, u: UserContext) -> IntentResult:
        b = self._bundle(u, "intent", f"Message: {text}", u.ctx_dict(text))
        return (
            await self.router.structured("intent", b, IntentResult, raw_message_id=u.raw_message_id)
        ).value

    async def extract(self, text: str, u: UserContext) -> ExtractionResult:
        b = self._bundle(u, "extract", f"Message: {text}", u.ctx_dict(text), **self._common(u))
        res = await self.router.structured(
            "extract", b, ExtractionResult, raw_message_id=u.raw_message_id
        )
        return amount_guard(text, currency_guard(text, res.value))

    async def categorize(
        self, txn: ExtractedTransaction, text: str, u: UserContext
    ) -> CategorizationResult:
        cat_list = "\n".join(f"{c.id}: {c.name}" for c in u.categories)
        b = self._bundle(
            u,
            "categorize",
            "Pick the category.",
            u.ctx_dict(text, category_hint=txn.category_hint, merchant=txn.merchant),
            transaction=json.dumps(txn.model_dump(exclude={"reasoning", "confidence"})),
            text=text,
            category_list=cat_list,
        )
        return (
            await self.router.structured(
                "categorize", b, CategorizationResult, raw_message_id=u.raw_message_id
            )
        ).value

    async def resolve_edit(self, text: str, u: UserContext) -> EditResolution:
        b = self._bundle(
            u,
            "edit",
            f"Message: {text}",
            u.ctx_dict(text),
            recent=_recent_block(u.recent),
            today=u.today,
        )
        return (
            await self.router.structured("edit", b, EditResolution, raw_message_id=u.raw_message_id)
        ).value

    async def resolve_delete(self, text: str, u: UserContext) -> DeleteResolution:
        b = self._bundle(
            u, "delete", f"Message: {text}", u.ctx_dict(text), recent=_recent_block(u.recent)
        )
        return (
            await self.router.structured(
                "delete", b, DeleteResolution, raw_message_id=u.raw_message_id
            )
        ).value

    async def plan_query(self, text: str, u: UserContext) -> QueryPlan:
        b = self._bundle(u, "query", f"Question: {text}", u.ctx_dict(text), **self._common(u))
        return (
            await self.router.structured("query", b, QueryPlan, raw_message_id=u.raw_message_id)
        ).value

    async def receipt(self, image_url: str, caption: str, u: UserContext) -> ReceiptExtraction:
        stage = render(load_prompt("receipt"), text=caption, today=u.today)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system(u)},
            {"role": "system", "content": stage},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": caption or "Receipt photo"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ]
        b = PromptBundle(messages=messages, ctx=u.ctx_dict(caption), images=[image_url])
        return (
            await self.router.structured(
                "receipt", b, ReceiptExtraction, raw_message_id=u.raw_message_id
            )
        ).value

    async def summarize(self, data: dict[str, Any], u: UserContext) -> str:
        stage = render(load_prompt("summarize"), data=json.dumps(data, indent=1, default=str))
        b = PromptBundle(
            messages=build_messages(self._system(u), stage, "Write the recap."), ctx={"data": data}
        )
        return (await self.router.structured("summarize", b, Summary)).value.text
