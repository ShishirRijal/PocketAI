"""Data exports (§12.15): `export csv this month` -> a file + a signed, expiring
download link. CSV and JSON for spreadsheets/scripts, QIF for real finance
software (GnuCash, Quicken, Banktivity import it)."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from pocket.channels.base import OutboundMessage
from pocket.config import Settings
from pocket.core import money
from pocket.core.dates import period_range
from pocket.data.db import Database
from pocket.data.models import Transaction, User
from pocket.data.repositories import TransactionRepo

if TYPE_CHECKING:
    from pocket.core.commands import Command
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.wiring import Services

LINK_TTL_S = 24 * 3600
MEDIA = {"csv": "text/csv", "json": "application/json", "qif": "application/qif"}


def render_rows(rows: list[Transaction], user: User, fmt: str) -> str:
    tz = ZoneInfo(user.timezone)
    if fmt == "json":
        return json.dumps(
            [
                {
                    "id": t.id,
                    "occurred_at": t.occurred_at.astimezone(tz).isoformat(),
                    "amount": str(money.from_minor(t.amount_minor, t.currency)),
                    "currency": t.currency,
                    f"amount_{user.base_currency.lower()}": str(
                        money.from_minor(t.amount_base_minor, user.base_currency)
                    ),
                    "direction": t.direction,
                    "category": t.category.full_name if t.category else None,
                    "merchant": t.merchant,
                    "location": t.location,
                    "tags": [x.name for x in t.tags],
                    "note": t.note,
                }
                for t in rows
            ],
            indent=1,
            ensure_ascii=False,
        )
    if fmt == "qif":
        out = ["!Type:Bank"]
        for t in rows:
            amt = money.from_minor(t.amount_base_minor, user.base_currency)
            sign = "" if t.direction == "income" else "-"
            memo = [
                t.location and f"@ {t.location}",
                t.note,
                " ".join(f"#{x.name}" for x in t.tags),
            ]
            if t.currency != user.base_currency:
                memo.append(f"({money.fmt(t.amount_minor, t.currency)})")
            out += [
                f"D{t.occurred_at.astimezone(tz):%m/%d/%Y}",
                f"T{sign}{amt}",
                f"P{t.merchant or ''}",
                f"L{t.category.full_name if t.category else ''}",
                "M" + " ".join(filter(None, memo)),
                "^",
            ]
        return "\n".join(out) + "\n"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "id",
            "date",
            "amount",
            "currency",
            f"amount_{user.base_currency.lower()}",
            "direction",
            "category",
            "merchant",
            "location",
            "tags",
            "note",
        ]
    )
    for t in rows:
        w.writerow([
            t.id, t.occurred_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"), money.from_minor(t.amount_minor, t.currency),
            t.currency, money.from_minor(t.amount_base_minor, user.base_currency), t.direction,
            t.category.full_name if t.category else "", t.merchant or "", t.location or "", " ".join(x.name for x in t.tags), t.note or "",
        ])  # fmt: skip
    return buf.getvalue()


def export_to_file(db: Database, user_id: int, fmt: str, period: str, settings: Settings) -> Path:
    with db.session() as s:
        user = s.get(User, user_id)
        assert user is not None
        rng = period_range(period, user.timezone)
        rows = TransactionRepo(s).in_range(user_id, rng.start, rng.end)
        body = render_rows(rows, user, fmt)
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    name = (
        f"pocket-{period.replace('_', '-')}-{time.strftime('%Y%m%d')}-{secrets.token_hex(4)}.{fmt}"
    )
    path = settings.export_dir / name
    path.write_text(body)
    return path


def sign(settings: Settings, name: str, exp: int) -> str:
    key = (settings.secret_key or settings.admin_token or "dev").encode()
    return hmac.new(key, f"{name}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def link_for(settings: Settings, path: Path) -> str:
    exp = int(time.time()) + LINK_TTL_S
    return f"{settings.public_url.rstrip('/')}/exports/{path.name}?exp={exp}&sig={sign(settings, path.name, exp)}"


router = APIRouter(tags=["exports"])


@router.get("/exports/{name}", include_in_schema=False)
async def download(request: Request, name: str, exp: int, sig: str) -> FileResponse:
    settings: Settings = request.app.state.runtime.settings
    if exp < time.time() or not hmac.compare_digest(sig, sign(settings, name, exp)):
        raise HTTPException(403, "link expired or invalid")
    path = (settings.export_dir / name).resolve()
    if path.parent != settings.export_dir.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(
        path, media_type=MEDIA.get(path.suffix[1:], "application/octet-stream"), filename=name
    )


async def export_cmd(o: Orchestrator, t: Turn, cmd: Command) -> list[OutboundMessage]:
    fmt, period = cmd.args["format"], cmd.args["period"]
    rng = period_range(period, t.tz, t.now)
    n = len(TransactionRepo(t.s).in_range(t.user.id, rng.start, rng.end))
    if n == 0:
        return [OutboundMessage(text=f"Nothing to export for {rng.label.lower()}.")]
    # the export reads through its own session; flush ours so it sees everything
    t.s.flush()
    path = export_to_file(o.db, t.user.id, fmt, period, o.settings)
    url = link_for(o.settings, path)
    return [
        OutboundMessage(
            text=f"📦 {fmt.upper()} export, {rng.label} ({n} transactions). Link valid 24h:\n{url}",
            attachment_url=url if o.settings.public_url.startswith("https") else None,
        )
    ]


def install(services: Services) -> None:
    services.orchestrator.extra_commands["export"] = export_cmd
