"""Prompt assembly with caching in mind.

Provider-side prompt caching (Gemini implicit caching, OpenAI automatic caching,
Anthropic cache_control) only kicks in when the *prefix* of the request is
byte-identical between calls. So every request is laid out as:

    [system: persona + user profile + category list]   <- stable per user/day
    [system: stage instructions]                        <- stable per stage
    [user:   volatile context + the message]            <- changes every call

The stable block is also memoized here so we don't re-render it per stage.
"""

from __future__ import annotations

import hashlib
import re
from functools import cache
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).parent / "prompts"
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@cache
def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text().strip()


def render(template: str, **vars: Any) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in vars:
            raise KeyError(f"prompt variable {key!r} not provided")
        return str(vars[key])

    return _VAR.sub(sub, template)


class SystemPromptCache:
    def __init__(self, max_entries: int = 64):
        self._store: dict[str, str] = {}
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(user_id: int, base_currency: str, timezone: str, categories: list[str]) -> str:
        h = hashlib.sha256("|".join([str(user_id), base_currency, timezone, *categories]).encode())
        return h.hexdigest()[:16]

    def get(self, user_id: int, base_currency: str, timezone: str, categories: list[str]) -> str:
        k = self.key(user_id, base_currency, timezone, categories)
        if k in self._store:
            self.hits += 1
            return self._store[k]
        self.misses += 1
        text = render(
            load_prompt("system"),
            base_currency=base_currency,
            timezone=timezone,
            categories="\n".join(f"- {c}" for c in categories) or "- (none yet)",
        )
        if len(self._store) >= self.max_entries:
            self._store.pop(next(iter(self._store)))
        self._store[k] = text
        return text


def build_messages(system_text: str, stage_text: str, user_text: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": system_text},
        {"role": "system", "content": stage_text},
        {"role": "user", "content": user_text},
    ]


def with_cache_control(model: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic only caches what's explicitly marked. Gemini and OpenAI cache
    stable prefixes on their own, and litellm maps cache_control on gemini to an
    explicit context-cache create, which we don't want for tiny prompts."""
    if not model.startswith("anthropic/") or not messages:
        return messages
    first = messages[0]
    if first.get("role") != "system" or not isinstance(first.get("content"), str):
        return messages
    marked = {
        "role": "system",
        "content": [
            {"type": "text", "text": first["content"], "cache_control": {"type": "ephemeral"}}
        ],
    }
    return [marked, *messages[1:]]
