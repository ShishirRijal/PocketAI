import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest

from pocket.config import Settings
from pocket.llm.backends.fake import FakeBackend
from pocket.llm.router import RouterConfig
from pocket.services.fx import FxService, Rate
from pocket.wiring import build_services, ensure_owner

PURPOSES = ["intent", "extract", "categorize", "edit", "delete", "query", "summarize", "receipt"]


def rules_config(primary: str = "rules/v1") -> RouterConfig:
    chains = {
        p: {"primary": primary, "fallbacks": ["rules/v1"] if primary != "rules/v1" else []}
        for p in PURPOSES
    }
    return RouterConfig.from_dict({"router": chains})


class StaticFx(FxService):
    RATES: ClassVar = {("NPR", "EUR"): Decimal("0.0057339"), ("USD", "EUR"): Decimal("0.877")}

    def __init__(self):
        super().__init__()

    async def rate(self, from_cur, to_cur):
        if from_cur == to_cur:
            return Rate(Decimal(1), "identity", time.time())
        if (from_cur, to_cur) in self.RATES:
            return Rate(self.RATES[(from_cur, to_cur)], "ECB 2026-09-25", time.time())
        from pocket.services.fx import FxError

        raise FxError("nope")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 9, 28, 18, 14, tzinfo=UTC))  # 21:14 Tallinn, a Monday


@pytest.fixture
def fake():
    return FakeBackend()


# set POCKET_TEST_DATABASE_URL=postgresql+psycopg://... to run the suite on postgres
TEST_DB_URL = os.environ.get("POCKET_TEST_DATABASE_URL")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        env="test",
        database_url=TEST_DB_URL or f"sqlite:///{tmp_path / 'test.db'}",
        redis_url=None,
        owner_cli="cli:test",
        owner_whatsapp="whatsapp:+37255500000",
        admin_token="secret",
        verify_signatures=False,
        scheduler_enabled=False,
        export_dir=tmp_path / "exports",
        backup_dir=tmp_path / "backups",
        _env_file=None,
    )


@pytest.fixture
def services(settings, clock, fake):
    svc = build_services(
        settings,
        backends={"fake": fake},
        router_config=rules_config(),
        fx=StaticFx(),
        use_litellm=False,
    )
    if TEST_DB_URL:
        from pocket.data.db import Base

        Base.metadata.drop_all(svc.db.engine)
    svc.db.create_all()
    svc.orchestrator.clock = clock
    svc.user_id = ensure_owner(svc.db, settings)
    return svc


@pytest.fixture
def say(services):
    async def _say(text):
        replies = await services.orchestrator.handle_text(services.user_id, text)
        return "\n".join(r.text for r in replies)

    return _say
