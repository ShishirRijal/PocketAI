"""Buffers llm_calls rows for the message being handled and writes them after.

Writing them from the router directly would open a second DB transaction while
the orchestrator's is still open, which on sqlite means waiting on the write
lock. Buffering also means failed attempts get recorded even when the handling
transaction rolls back.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from decimal import Decimal
from typing import Any

from pocket.data.db import Database
from pocket.data.repositories import LLMCallRepo

log = logging.getLogger(__name__)

_buffer: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_call_buffer", default=None)


class CallLog:
    def __init__(self, db: Database):
        self.db = db

    def start(self) -> Token:
        return _buffer.set([])

    def sink(self, row: dict[str, Any]) -> None:
        buf = _buffer.get()
        if buf is not None:
            buf.append(row)
        else:
            self._write([row])

    def flush(self, token: Token) -> list[dict[str, Any]]:
        rows = _buffer.get() or []
        _buffer.reset(token)
        if rows:
            self._write(rows)
        return rows

    def _write(self, rows: list[dict[str, Any]]) -> None:
        try:
            with self.db.session() as s:
                repo = LLMCallRepo(s)
                for r in rows:
                    r = dict(r)
                    if r.get("cost_usd") is not None:
                        r["cost_usd"] = Decimal(str(r["cost_usd"]))
                    repo.log(**r)
        except Exception:
            log.exception("failed to persist %d llm call rows", len(rows))
