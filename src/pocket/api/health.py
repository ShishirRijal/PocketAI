from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: DB answers, Redis answers (if configured), and at least one
    model has credentials for extraction."""
    runtime = request.app.state.runtime
    checks: dict[str, object] = {}
    ok = True
    try:
        with runtime.services.db.session() as s:
            s.execute(text("select 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"fail: {e}"
        ok = False
    if runtime.services.redis is not None:
        try:
            await runtime.services.redis.ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"fail: {e}"
            ok = False
    models = runtime.services.router.usable_models("extract")
    checks["llm_extract_models"] = models
    if not models:
        ok = False
    return JSONResponse(
        {"status": "ok" if ok else "fail", "checks": checks}, status_code=200 if ok else 503
    )
