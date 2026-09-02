"""Real providers through the LiteLLM SDK (Gemini, Grok, OpenAI, Anthropic, Ollama, ...)."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from pydantic import BaseModel

from pocket.llm.cache import with_cache_control
from pocket.llm.router import (
    AuthError,
    BackendResponse,
    ContextTooLong,
    LLMError,
    ModelNotFound,
    PromptBundle,
    RateLimited,
    SchemaParseError,
    ServerError,
    Timeout,
)

log = logging.getLogger(__name__)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve $defs/$ref in place. Gemini's schema dialect is picky about refs."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return walk(copy.deepcopy(defs[name]))
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def _is_gemini3(model: str) -> bool:
    name = model.split("/", 1)[-1]
    return name.startswith(("gemini-3", "gemini-flash", "gemini-pro"))


class LiteLLMBackend:
    free = False

    def __init__(self) -> None:
        import litellm

        litellm.drop_params = True  # silently drop params a provider doesn't take
        litellm.suppress_debug_info = True
        self._litellm = litellm

    def available(self, model: str) -> bool:
        try:
            env = self._litellm.validate_environment(model)
        except Exception:
            return False
        return bool(env.get("keys_in_environment"))

    def _response_format(self, model: str, schema: type[BaseModel]) -> dict[str, Any]:
        try:
            native = self._litellm.supports_response_schema(model)
        except Exception:
            native = False
        if native:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": inline_refs(schema.model_json_schema()),
                    "strict": False,
                },
            }
        return {"type": "json_object"}

    async def complete(
        self,
        model: str,
        purpose: str,
        prompt: PromptBundle,
        schema: type[BaseModel],
        timeout: float,
    ) -> BackendResponse:
        exc = self._litellm.exceptions
        messages = with_cache_control(model, list(prompt.messages))
        rf = self._response_format(model, schema)
        if rf["type"] == "json_object":
            # no native schema support (e.g. grok via some routes): describe it in-prompt
            messages = [
                *messages,
                {
                    "role": "system",
                    "content": "Reply with only a JSON object matching this JSON schema:\n"
                    + json.dumps(inline_refs(schema.model_json_schema())),
                },
            ]
        kwargs: dict[str, Any] = {}
        if not _is_gemini3(model):
            # gemini 3+ deprecates sampling params and wants guidance in the prompt
            kwargs["temperature"] = 0
        try:
            resp = await self._litellm.acompletion(
                model=model,
                messages=messages,
                response_format=rf,
                timeout=timeout,
                num_retries=0,
                **kwargs,
            )
        except exc.RateLimitError as e:
            raise RateLimited(str(e)[:500]) from e
        except exc.ContextWindowExceededError as e:
            raise ContextTooLong(str(e)[:500]) from e
        except exc.Timeout as e:
            raise Timeout(str(e)[:500]) from e
        except (exc.AuthenticationError, exc.PermissionDeniedError) as e:
            raise AuthError(str(e)[:500]) from e
        except exc.JSONSchemaValidationError as e:
            raise SchemaParseError(str(e)[:500]) from e
        except exc.NotFoundError as e:
            raise ModelNotFound(str(e)[:500]) from e
        except (
            exc.InternalServerError,
            exc.ServiceUnavailableError,
            exc.APIConnectionError,
            exc.BadGatewayError,
        ) as e:
            raise ServerError(str(e)[:500]) from e
        except exc.APIError as e:
            raise LLMError(str(e)[:500]) from e

        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        try:
            cost = float(self._litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            cost = 0.0
        return BackendResponse(
            content=content,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            cost_usd=cost,
        )


async def transcribe(model: str, audio: bytes, filename: str) -> str:
    """Voice notes -> text (whisper-1 / gemini via litellm)."""
    import litellm

    resp = await litellm.atranscription(model=model, file=(filename, audio))
    return str(getattr(resp, "text", "") or "")
