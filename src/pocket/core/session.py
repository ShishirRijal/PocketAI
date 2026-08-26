"""Short-lived conversational state: recent transactions and the last action
(for undo). 15 minute TTL. Redis when configured, otherwise a DB table."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from pocket.data.db import Database, utcnow
from pocket.data.models import SessionRow


class LastAction(BaseModel):
    kind: Literal["add", "edit", "delete"]
    transaction_ids: list[int]
    # for edits: the version rows written, so undo can reverse exactly those
    version_ids: list[int] = Field(default_factory=list)
    at: datetime


class Session(BaseModel):
    user_id: int
    recent_transactions: list[int] = Field(default_factory=list)
    last_action: LastAction | None = None
    # what the user last saw as a numbered list ("edit" with no args), so
    # "edit 2 ..." refers to the same row they looked at
    shown_list: list[int] = Field(default_factory=list)

    def remember(self, txn_ids: list[int], limit: int = 5) -> None:
        ids = [i for i in txn_ids if i not in self.recent_transactions]
        self.recent_transactions = (list(reversed(ids)) + self.recent_transactions)[:limit]


class SessionStore(Protocol):
    async def get(self, user_id: int) -> Session: ...

    async def save(self, session: Session) -> None: ...

    async def clear(self, user_id: int) -> None: ...


class MemorySessionStore:
    def __init__(self, ttl: timedelta = timedelta(minutes=15)):
        self.ttl = ttl
        self._data: dict[int, tuple[datetime, str]] = {}

    async def get(self, user_id: int) -> Session:
        hit = self._data.get(user_id)
        if hit and hit[0] > utcnow():
            return Session.model_validate_json(hit[1])
        return Session(user_id=user_id)

    async def save(self, session: Session) -> None:
        self._data[session.user_id] = (utcnow() + self.ttl, session.model_dump_json())

    async def clear(self, user_id: int) -> None:
        self._data.pop(user_id, None)


class DBSessionStore:
    def __init__(self, db: Database, ttl: timedelta = timedelta(minutes=15)):
        self.db = db
        self.ttl = ttl

    async def get(self, user_id: int) -> Session:
        with self.db.session() as s:
            row = s.get(SessionRow, user_id)
            if row and row.expires_at > utcnow():
                return Session.model_validate(row.data)
        return Session(user_id=user_id)

    async def save(self, session: Session) -> None:
        with self.db.session() as s:
            row = s.get(SessionRow, session.user_id)
            data = json.loads(session.model_dump_json())
            if row is None:
                s.add(
                    SessionRow(user_id=session.user_id, data=data, expires_at=utcnow() + self.ttl)
                )
            else:
                row.data = data
                row.expires_at = utcnow() + self.ttl

    async def clear(self, user_id: int) -> None:
        with self.db.session() as s:
            row = s.get(SessionRow, user_id)
            if row:
                s.delete(row)


class RedisSessionStore:
    def __init__(self, redis, ttl: timedelta = timedelta(minutes=15)):  # redis.asyncio.Redis
        self.redis = redis
        self.ttl = ttl

    @staticmethod
    def _key(user_id: int) -> str:
        return f"session:{user_id}"

    async def get(self, user_id: int) -> Session:
        raw = await self.redis.get(self._key(user_id))
        return Session.model_validate_json(raw) if raw else Session(user_id=user_id)

    async def save(self, session: Session) -> None:
        await self.redis.set(
            self._key(session.user_id), session.model_dump_json(), ex=int(self.ttl.total_seconds())
        )

    async def clear(self, user_id: int) -> None:
        await self.redis.delete(self._key(user_id))
