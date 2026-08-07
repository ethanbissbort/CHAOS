"""Database engine, session handling and schema creation.

PostgreSQL + PostGIS is the deployment target. SQLite is supported so the
platform, its tests and the simulator run without external services -- this
matters for the secondary control node and for bench testing (SDD section 19).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from chaos.config import Settings, get_settings
from chaos.models.base import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _prepare_sqlite(url: str) -> None:
    """Make sure the parent directory of a SQLite file database exists."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    raw = url[len(prefix) :]
    if raw and raw != ":memory:":
        Path(raw).expanduser().parent.mkdir(parents=True, exist_ok=True)


def build_engine(settings: Settings | None = None, url: str | None = None) -> Engine:
    settings = settings or get_settings()
    database_url = url or settings.database_url
    _prepare_sqlite(database_url)

    kwargs: dict = {"echo": settings.sql_echo, "future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True

    engine = create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def configure(engine: Engine) -> None:
    """Point the process at a specific engine (used by tests and the CLI)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine | None = None) -> None:
    """Create every table. Import side effects register all model modules."""
    import chaos.models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(engine or get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope around a series of operations."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
