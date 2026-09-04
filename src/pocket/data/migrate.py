"""Programmatic alembic upgrade, used at startup and by `pocket migrate`."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from pocket.data.db import Database

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["skip_logging"] = True
    return cfg


def upgrade(db: Database, revision: str = "head") -> None:
    cfg = alembic_config(db.url)
    with db.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)
