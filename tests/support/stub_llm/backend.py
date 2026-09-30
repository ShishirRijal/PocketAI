"""`rules/*` models: the offline parser exposed as an LLM backend."""

from __future__ import annotations

from pydantic import BaseModel

from pocket.llm.router import BackendResponse, LLMError, PromptBundle
from pocket.llm.rules import parser


class RulesBackend:
    free = True

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
        ctx = prompt.ctx
        text = ctx.get("text", "")
        match purpose:
            case "intent":
                out: BaseModel = parser.classify_intent(text)
            case "extract":
                out = parser.extract(text, ctx)
            case "categorize":
                out = parser.categorize(ctx)
            case "edit":
                out = parser.resolve_edit(text, ctx)
            case "delete":
                out = parser.resolve_delete(text, ctx)
            case "query":
                out = parser.plan_query(text, ctx)
            case _:
                raise LLMError(f"rules backend can't do {purpose!r}")
        return BackendResponse(content=out.model_dump(mode="json"), cost_usd=0.0)
