"""Builds the object graph from settings. One place, so the api, the worker, the
CLI and the tests all get the same wiring."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pocket.config import Settings
from pocket.core.calllog import CallLog
from pocket.core.orchestrator import Orchestrator
from pocket.core.ratelimit import RateLimiter
from pocket.core.session import DBSessionStore, RedisSessionStore, SessionStore
from pocket.data.db import Database
from pocket.data.repositories import LLMCallRepo, UserRepo
from pocket.llm.backends.rules import RulesBackend
from pocket.llm.quota import QuotaTracker
from pocket.llm.router import Backend, LLMRouter, RouterConfig
from pocket.llm.stages import Pipeline
from pocket.services.fx import FxService

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    router: LLMRouter
    pipeline: Pipeline
    sessions: SessionStore
    fx: FxService
    orchestrator: Orchestrator
    call_log: CallLog
    rate_limiter: RateLimiter
    redis: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


class DailyCostGuard:
    """True once today's (UTC) LLM spend reaches the cap. Cached for a few seconds
    so we don't sum llm_calls on every single call."""

    def __init__(self, db: Database, cap_usd: float, ttl_s: float = 5.0):
        self.db = db
        self.cap = cap_usd
        self.ttl = ttl_s
        self._at = 0.0
        self._val = False

    def __call__(self) -> bool:
        if self.cap <= 0:
            return False
        now = time.monotonic()
        if now - self._at > self.ttl:
            start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            with self.db.session() as s:
                self._val = LLMCallRepo(s).cost_since(start) >= self.cap
            self._at = now
        return self._val


def build_services(
    settings: Settings,
    *,
    backends: dict[str, Backend] | None = None,
    default_backend: Backend | None = None,
    router_config: RouterConfig | None = None,
    fx: FxService | None = None,
    db: Database | None = None,
    use_litellm: bool = True,
) -> Services:
    db = db or Database(settings.database_url)
    redis_sync = redis_async = None
    if settings.redis_url:
        import redis as redis_lib
        import redis.asyncio as redis_asyncio

        redis_sync = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        redis_async = redis_asyncio.Redis.from_url(settings.redis_url, decode_responses=True)

    cfg = router_config or RouterConfig.from_yaml(settings.llm_config_path)
    call_log = CallLog(db)
    all_backends: dict[str, Backend] = {"rules": RulesBackend()}
    if backends:
        all_backends.update(backends)
    if default_backend is None and use_litellm:
        from pocket.llm.backends.litellm_backend import LiteLLMBackend

        default_backend = LiteLLMBackend()
    router = LLMRouter(
        cfg,
        all_backends,
        default_backend,
        quota=QuotaTracker(cfg.quotas, redis=redis_sync),
        call_sink=call_log.sink,
        cost_guard=DailyCostGuard(db, settings.llm_daily_cost_cap_usd),
        timeout=settings.llm_timeout_s,
    )
    pipeline = Pipeline(router)
    from datetime import timedelta

    ttl = timedelta(minutes=settings.session_ttl_minutes)
    sessions: SessionStore = (
        RedisSessionStore(redis_async, ttl) if redis_async else DBSessionStore(db, ttl)
    )
    fx = fx or FxService(cache_hours=settings.fx_cache_hours, redis=redis_async)
    limiter = RateLimiter(redis=redis_sync)
    query_limiter = limiter.scoped(settings.query_rate_limit_per_min, 60)
    orch = Orchestrator(
        db, pipeline, sessions, fx, settings, call_log=call_log, query_limiter=query_limiter
    )
    services = Services(
        settings=settings,
        db=db,
        router=router,
        pipeline=pipeline,
        sessions=sessions,
        fx=fx,
        orchestrator=orch,
        call_log=call_log,
        rate_limiter=limiter,
        redis=redis_async,
    )
    _install_features(services)
    return services


def _install_features(services: Services) -> None:
    """Optional features hook themselves into the orchestrator."""
    import importlib

    for mod in (
        "pocket.services.budgets",
        "pocket.services.recurring",
        "pocket.services.exports",
        "pocket.services.digests",
        "pocket.services.lending",
        "pocket.services.people",
        "pocket.services.media",
    ):
        try:
            m = importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if e.name == mod:
                continue
            raise
        m.install(services)


def ensure_owner(db: Database, settings: Settings) -> int:
    """Make sure the owner user exists and every configured identity maps to it.
    Returns the owner's user id."""
    with db.session() as s:
        repo = UserRepo(s)
        users = repo.all()
        user = (
            users[0]
            if users
            else repo.create(
                name="owner",
                base_currency=settings.default_base_currency,
                timezone=settings.default_timezone,
            )
        )
        for channel, ident in (
            ("whatsapp", settings.owner_whatsapp),
            ("telegram", settings.owner_telegram),
            ("discord", settings.owner_discord),
            ("cli", settings.owner_cli),
        ):
            if ident:
                repo.add_identity(user, channel, ident)
        return user.id
