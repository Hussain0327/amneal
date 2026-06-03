"""SQLite session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from config.settings import get_settings
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        s.ensure_dirs()
        url = f"sqlite:///{s.sqlite_path.as_posix()}"
        _engine = create_engine(url, echo=False)
    return _engine


def init_db() -> None:
    """Create tables. Import models so SQLModel.metadata sees them."""
    from regwatch.store import models  # noqa: F401  (registers tables)

    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_for_tests() -> None:
    """Tests use this to swap in a temp DB. Resets the cached engine."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
