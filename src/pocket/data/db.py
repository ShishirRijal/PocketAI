"""Engine + session factory. Sync SQLAlchemy on purpose: one user, short queries,
and it keeps repositories and alembic boring."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, Engine, TypeDecorator, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC, always hands back aware UTC. SQLite drops tzinfo otherwise."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):  # type: ignore[override]
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


class Database:
    def __init__(self, url: str):
        self.url = url
        self.engine = make_engine(url)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def create_all(self) -> None:
        """Test/dev shortcut. Prod goes through alembic."""
        from pocket.data import models  # noqa: F401

        Base.metadata.create_all(self.engine)
