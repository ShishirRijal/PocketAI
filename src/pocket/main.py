"""FastAPI app factory. `uvicorn pocket.main:app` or `pocket serve`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from pocket import __version__
from pocket.api import admin, health, webhooks
from pocket.config import Settings, get_settings
from pocket.logging_setup import setup_logging
from pocket.runtime import Runtime, build_runtime
from pocket.services import exports
from pocket.web import dashboard

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    run_worker: bool | None = None,
    run_scheduler: bool | None = None,
    **overrides: Any,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level, settings.log_json)
        rt = runtime or build_runtime(settings, **overrides)
        app.state.runtime = rt
        # with redis streams the worker is its own process; inline, it lives here
        worker = run_worker if run_worker is not None else settings.queue_backend == "inline"
        sched = run_scheduler if run_scheduler is not None else settings.scheduler_enabled
        await rt.start(worker=worker, scheduler=sched)
        log.info("pocket up", extra={"env": settings.env, "worker": worker, "scheduler": sched})
        try:
            yield
        finally:
            await rt.stop()

    prod = settings.env == "prod"
    app = FastAPI(
        title="Pocket",
        version=__version__,
        lifespan=lifespan,
        # no public API explorer in prod; the api is documented in the README
        docs_url=None if prod else "/docs",
        redoc_url=None,
        openapi_url=None if prod else "/openapi.json",
    )
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.include_router(exports.router)
    app.include_router(dashboard.router)
    return app


def __getattr__(name: str) -> Any:
    # lazy `app` so importing this module doesn't read settings at import time
    if name == "app":
        return create_app()
    raise AttributeError(name)
