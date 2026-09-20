"""Background jobs (APScheduler, in-process).

- every 2 min   re-queue messages stuck on llm_unavailable / never processed
- every 10 min  post due recurring transactions
- hourly        weekly digest (fires when it's Sunday 20:00 in the user's tz)
- nightly       sqlite backup (+ optional Azure Blob upload), purge old llm payloads
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from pocket.data.db import utcnow
from pocket.data.repositories import LLMCallRepo, RawMessageRepo

if TYPE_CHECKING:
    from pocket.runtime import Runtime

log = logging.getLogger(__name__)


async def reprocess_stuck(rt: Runtime) -> int:
    with rt.services.db.session() as s:
        ids = [r.id for r in RawMessageRepo(s).unprocessed(timedelta(minutes=2))]
    for i in ids:
        await rt.queue.enqueue(i)
    if ids:
        log.info("re-queued %d stuck messages", len(ids))
    return len(ids)


def purge_llm_payloads(rt: Runtime, days: int = 30) -> int:
    """§9: full request/response payloads kept 30 days, aggregates forever."""
    with rt.services.db.session() as s:
        n = LLMCallRepo(s).purge_payloads(utcnow() - timedelta(days=days))
    if n:
        log.info("purged payloads of %d old llm calls", n)
    return n


def purge_exports(rt: Runtime, max_age_h: float = 48) -> int:
    """Export links live 24h; the files don't need to outlive them by much."""
    import time

    folder = rt.settings.export_dir
    if not folder.exists():
        return 0
    cutoff = time.time() - max_age_h * 3600
    old = [p for p in folder.iterdir() if p.is_file() and p.stat().st_mtime < cutoff]
    for p in old:
        p.unlink()
    return len(old)


def purge_deleted(rt: Runtime) -> int:
    """§7.3: deletes are soft; optionally really remove them after N days."""
    days = rt.settings.purge_deleted_after_days
    if days <= 0:
        return 0
    from sqlalchemy import delete

    from pocket.data.models import Transaction

    with rt.services.db.session() as s:
        res = s.execute(
            delete(Transaction).where(
                Transaction.deleted_at.is_not(None),
                Transaction.deleted_at < utcnow() - timedelta(days=days),
            )
        )
        n = int(getattr(res, "rowcount", 0) or 0)
    if n:
        log.info("purged %d soft-deleted transactions older than %d days", n, days)
    return n


async def run_digests(rt: Runtime) -> None:
    from pocket.services.digests import send_due_digests

    await send_due_digests(rt)


async def run_recurring(rt: Runtime) -> None:
    from pocket.services.recurring import post_due

    await post_due(rt)


async def run_backup(rt: Runtime) -> None:
    from pocket.services.backups import backup_now

    await backup_now(rt.settings, rt.services.db)


def build_scheduler(rt: Runtime) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    common = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    sched.add_job(reprocess_stuck, IntervalTrigger(minutes=2), args=[rt], id="reprocess", **common)
    sched.add_job(run_recurring, IntervalTrigger(minutes=10), args=[rt], id="recurring", **common)
    sched.add_job(run_digests, CronTrigger(minute=2), args=[rt], id="digest", **common)
    sched.add_job(run_backup, CronTrigger(hour=1, minute=30), args=[rt], id="backup", **common)
    sched.add_job(
        purge_llm_payloads, CronTrigger(hour=2, minute=10), args=[rt], id="purge", **common
    )
    sched.add_job(
        purge_deleted, CronTrigger(day=1, hour=3), args=[rt], id="purge_deleted", **common
    )
    return sched
