"""Admin & debugging endpoints (§9). Bearer token = POCKET_ADMIN_TOKEN.

- GET  /admin                     recent messages, outcomes, links to replay
- POST /admin/replay/{raw_id}     re-run a stored message (dry run unless ?commit=true)
- GET  /admin/cost                LLM spend by day/model (HTML); /admin/cost.json
- GET  /admin/llm_calls           recent calls with payloads
- GET  /admin/quota               free-tier counters
- POST /admin/reload              reload config/llm.yaml
- POST /admin/reprocess           retry messages stuck on llm_unavailable
"""

from __future__ import annotations

import html
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from pocket.data.db import utcnow
from pocket.data.models import LLMCall, RawMessage
from pocket.data.repositories import LLMCallRepo, RawMessageRepo
from pocket.llm.router import RouterConfig
from pocket.runtime import Runtime


def rt(request: Request) -> Runtime:
    return request.app.state.runtime


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None, description="for opening pages in a browser"),
) -> None:
    s = rt(request).settings
    if not s.admin_token:
        if s.env == "prod":
            raise HTTPException(403, "admin disabled: set POCKET_ADMIN_TOKEN")
        return
    supplied = token or (authorization or "").removeprefix("Bearer ").strip()
    if supplied != s.admin_token:
        raise HTTPException(401, "bad admin token")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#fbfbf9;--fg:#1d1d1b;--mut:#6b6b66;--line:#e4e3de;--acc:#2f6f4f;--bad:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--mut:#9a9993;--line:#2c2c2a;--acc:#7cc59b;--bad:#f2b8b5}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:24px 16px}}
h1{{font-size:20px;margin:0 0 4px}} .mut{{color:var(--mut)}} table{{border-collapse:collapse;width:100%;margin-top:16px}}
td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}} th{{color:var(--mut);font-weight:500}}
.num{{text-align:right;font-variant-numeric:tabular-nums}} .bar{{height:8px;background:var(--acc);border-radius:2px}}
.bad{{color:var(--bad)}} a{{color:var(--acc)}} nav a{{margin-right:12px}} code{{font-size:12px}}
.wrap{{overflow-x:auto}}
</style></head><body><nav class="mut"><a href="/admin{q}">messages</a><a href="/admin/cost{q}">cost</a><a href="/app{q}">dashboard</a></nav>
<h1>{title}</h1>{body}</body></html>"""


def page(title: str, body: str, token: str | None) -> HTMLResponse:
    q = f"?token={html.escape(token)}" if token else ""
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body, q=q))


@router.get("", response_class=HTMLResponse)
async def index(request: Request, token: str | None = None, limit: int = 50) -> HTMLResponse:
    with rt(request).services.db.session() as s:
        rows = s.scalars(select(RawMessage).order_by(RawMessage.id.desc()).limit(limit)).all()
        items = [
            (
                r.id,
                r.channel,
                r.received_at,
                r.text or ("[media]" if r.media_json else ""),
                r.outcome,
            )
            for r in rows
        ]
    trs = "".join(
        f"<tr><td class=num>{i}</td><td>{c}</td><td class=mut>{t:%Y-%m-%d %H:%M}</td>"
        f"<td>{html.escape(txt[:120])}</td><td class='{'bad' if o in ('llm_unavailable', 'cost_cap') else ''}'>{o or '…'}</td>"
        f"<td><form method=post action='/admin/replay/{i}?token={html.escape(token or '')}'><button>replay</button></form></td></tr>"
        for i, c, t, txt, o in items
    )
    body = (
        "<p class=mut>Every inbound message, verbatim. Replay is a dry run.</p><div class=wrap><table>"
        f"<tr><th>#</th><th>channel</th><th>received (UTC)</th><th>text</th><th>outcome</th><th></th></tr>{trs}</table></div>"
    )
    return page("Pocket · messages", body, token)


@router.post("/replay/{raw_id}")
async def replay(request: Request, raw_id: int, commit: bool = False) -> dict[str, Any]:
    try:
        return await rt(request).services.orchestrator.replay(raw_id, commit=commit)
    except KeyError:
        raise HTTPException(404, "no such message") from None


def _cost_data(request: Request, days: int) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    with rt(request).services.db.session() as s:
        usage = LLMCallRepo(s).usage_by_day_model(since)
    by_day: dict[str, float] = {}
    for u in usage:
        by_day[u["day"]] = by_day.get(u["day"], 0) + u["cost_usd"]
    return {
        "days": days,
        "total_usd": round(sum(u["cost_usd"] for u in usage), 6),
        "calls": sum(u["calls"] for u in usage),
        "by_day": dict(sorted(by_day.items())),
        "rows": usage,
        "daily_cap_usd": rt(request).settings.llm_daily_cost_cap_usd,
    }


@router.get("/cost.json")
async def cost_json(request: Request, days: int = 30) -> dict[str, Any]:
    return _cost_data(request, days)


@router.get("/cost", response_class=HTMLResponse)
async def cost_page(request: Request, days: int = 30, token: str | None = None) -> HTMLResponse:
    d = _cost_data(request, days)
    peak = max(d["by_day"].values(), default=0) or 1
    day_rows = "".join(
        f"<tr><td>{day}</td><td class=num>${v:.4f}</td><td style='width:50%'><div class=bar style='width:{100 * v / peak:.1f}%'></div></td></tr>"
        for day, v in sorted(d["by_day"].items(), reverse=True)
    )
    model_rows = "".join(
        f"<tr><td>{u['day']}</td><td><code>{html.escape(u['model'])}</code></td><td class=num>{u['calls']}</td>"
        f"<td class=num>{u['calls'] - u['ok']}</td><td class=num>{u['prompt_tokens']:,}/{u['completion_tokens']:,}</td>"
        f"<td class=num>{u['avg_latency_ms']} ms</td><td class=num>${u['cost_usd']:.5f}</td></tr>"
        for u in d["rows"]
    )
    body = (
        f"<p class=mut>Last {days} days: <b>${d['total_usd']:.4f}</b> over {d['calls']} calls · daily cap ${d['daily_cap_usd']:.2f}</p>"
        f"<div class=wrap><table><tr><th>day</th><th class=num>cost</th><th></th></tr>{day_rows}</table></div>"
        "<div class=wrap><table><tr><th>day</th><th>model</th><th class=num>calls</th><th class=num>failed</th>"
        f"<th class=num>tokens in/out</th><th class=num>avg latency</th><th class=num>cost</th></tr>{model_rows}</table></div>"
    )
    return page("Pocket · LLM cost", body, token)


@router.get("/llm_calls")
async def llm_calls(
    request: Request, limit: int = 20, raw_message_id: int | None = None
) -> list[dict[str, Any]]:
    with rt(request).services.db.session() as s:
        q = select(LLMCall).order_by(LLMCall.id.desc()).limit(min(limit, 200))
        if raw_message_id:
            q = q.where(LLMCall.raw_message_id == raw_message_id)
        return [
            {
                "id": c.id,
                "raw_message_id": c.raw_message_id,
                "purpose": c.purpose,
                "model": c.model,
                "success": c.success,
                "error": c.error,
                "latency_ms": c.latency_ms,
                "cost_usd": float(c.cost_usd or 0),
                "tokens": [c.prompt_tokens, c.completion_tokens],
                "request": c.request_json,
                "response": c.response_json,
                "at": c.created_at.isoformat(),
            }
            for c in s.scalars(q).all()
        ]


@router.get("/quota")
async def quota(request: Request) -> dict[str, Any]:
    r = rt(request).services.router
    return {
        "quota": r.quota.snapshot(),
        "usable": {p: r.usable_models(p) for p in ("intent", "extract", "query", "receipt")},
    }


@router.post("/reload")
async def reload(request: Request) -> dict[str, Any]:
    runtime = rt(request)
    cfg = RouterConfig.from_yaml(runtime.settings.llm_config_path)
    runtime.services.router.config = cfg
    runtime.services.router.quota.limits = cfg.quotas
    return {"reloaded": True, "chains": {k: v.models for k, v in cfg.chains.items()}}


@router.post("/reprocess")
async def reprocess(request: Request, older_than_s: int = 60) -> dict[str, Any]:
    runtime = rt(request)
    with runtime.services.db.session() as s:
        ids = [r.id for r in RawMessageRepo(s).unprocessed(timedelta(seconds=older_than_s))]
    for i in ids:
        await runtime.queue.enqueue(i)
    return {"requeued": ids}
