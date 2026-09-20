from datetime import timedelta

from sqlalchemy import select

from pocket.data.db import utcnow
from pocket.data.models import RawMessage
from pocket.llm.backends.fake import BrokenBackend
from pocket.llm.router import RouterConfig
from pocket.runtime import build_runtime
from pocket.services.scheduler import build_scheduler, reprocess_stuck


def test_scheduler_jobs(settings, services):
    rt = build_runtime(settings, services=services)
    ids = {j.id for j in build_scheduler(rt).get_jobs()}
    assert ids == {
        "reprocess",
        "recurring",
        "digest",
        "backup",
        "purge",
        "purge_deleted",
        "purge_exports",
    }


async def test_outage_then_recovery(settings, services):
    """Every model down: the message is kept, the user is told, and the
    reprocess job picks it up once a model is back."""
    rt = build_runtime(settings, services=services)
    broken = BrokenBackend()
    services.router.backends["down"] = broken
    services.router.config = RouterConfig.from_dict(
        {
            "router": {
                p: {"primary": "down/x"}
                for p in ["intent", "extract", "categorize", "edit", "delete", "query"]
            }
        }
    )
    from pocket.channels.base import InboundMessage

    res = await rt.ingestor.ingest(
        InboundMessage(
            channel="cli", channel_msg_id="m1", user_channel_id="cli:test", text="lunch 12"
        ),
        enqueue=False,
    )
    replies = await rt.dispatcher.process(res.raw_message_id)
    assert "saved your message" in replies[0].text
    with services.db.session() as s:
        raw = s.get(RawMessage, res.raw_message_id)
        assert raw.outcome == "llm_unavailable"
        raw.received_at = utcnow() - timedelta(minutes=5)

    # providers come back (here: the offline parser)
    services.router.config = RouterConfig.from_dict(
        {
            "router": {
                p: {"primary": "rules/v1"}
                for p in ["intent", "extract", "categorize", "edit", "delete", "query"]
            }
        }
    )
    assert await reprocess_stuck(rt) == 1
    raw_id = rt.queue._q.get_nowait()
    replies = await rt.dispatcher.process(raw_id)
    assert "✅ Logged €12.00" in replies[0].text
    with services.db.session() as s:
        assert s.scalars(select(RawMessage.outcome)).one() == "added"
