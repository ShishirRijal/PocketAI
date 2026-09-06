"""Nightly backups (§10). SQLite: online backup API (safe while the app is
writing), gzip, keep the last N, optionally PUT to Azure Blob with a container
SAS URL. Postgres: pg_dump if it's on PATH.

    pocket backup                      # now
    pocket restore data/backups/x.gz   # stop the app first
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from pocket.config import Settings
from pocket.data.db import Database

log = logging.getLogger(__name__)


def _sqlite_path(url: str) -> Path:
    return Path(url.removeprefix("sqlite:///"))


def _snapshot_sqlite(src: Path, dest_gz: Path) -> None:
    tmp = dest_gz.with_suffix("")
    with sqlite3.connect(src) as source, sqlite3.connect(tmp) as target:
        source.backup(target)
    with open(tmp, "rb") as f_in, gzip.open(dest_gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp.unlink()


def _rotate(folder: Path, keep: int) -> list[Path]:
    files = sorted(folder.glob("pocket-*.gz"))
    removed = files[:-keep] if keep > 0 else []
    for f in removed:
        f.unlink()
    return removed


def blob_url(container_sas_url: str, name: str) -> str:
    parts = urlsplit(container_sas_url)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/") + "/" + name, parts.query, "")
    )


async def upload_azure(container_sas_url: str, path: Path) -> None:
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.put(
            blob_url(container_sas_url, path.name),
            content=path.read_bytes(),
            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "application/gzip"},
        )
        r.raise_for_status()


async def backup_now(settings: Settings, db: Database) -> str:
    folder = settings.backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"{int(now * 1000) % 1000:03d}"
    if settings.is_sqlite:
        dest = folder / f"pocket-{stamp}.db.gz"
        await asyncio.to_thread(_snapshot_sqlite, _sqlite_path(db.url), dest)
    else:
        if not shutil.which("pg_dump"):
            raise RuntimeError("pg_dump not found; install postgresql-client for postgres backups")
        dest = folder / f"pocket-{stamp}.sql.gz"
        dump = await asyncio.to_thread(
            subprocess.run,
            ["pg_dump", "--no-owner", db.url.replace("+psycopg", "")],
            capture_output=True,
            check=True,
        )
        dest.write_bytes(gzip.compress(dump.stdout))
    removed = _rotate(folder, settings.backup_keep)
    msg = f"backup written: {dest} ({dest.stat().st_size / 1024:.0f} KiB), rotated {len(removed)}"
    if settings.backup_azure_sas_url:
        try:
            await upload_azure(settings.backup_azure_sas_url, dest)
            msg += ", uploaded to azure"
        except Exception as e:
            log.error("azure upload failed: %s", e)
            msg += f", AZURE UPLOAD FAILED: {e}"
    log.info(msg)
    return msg


def restore(settings: Settings, file: str) -> str:
    """Replace the sqlite db with a backup. Keeps the current file as *.before-restore."""
    if not settings.is_sqlite:
        return "postgres: gunzip -c FILE | psql $DATABASE_URL"
    src = Path(file)
    if not src.is_file():
        raise FileNotFoundError(src)
    target = _sqlite_path(settings.database_url)
    if target.exists():
        safety = target.with_suffix(
            target.suffix + f".before-restore-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(target, safety)
    for side in (Path(f"{target}-wal"), Path(f"{target}-shm")):
        side.unlink(missing_ok=True)
    opener = gzip.open if src.suffix == ".gz" else open
    with opener(src, "rb") as f_in, open(target, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    with sqlite3.connect(target) as conn:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    return f"restored {src} -> {target} (integrity: {ok})"
