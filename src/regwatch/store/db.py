"""SQLite session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from config.settings import get_settings
from sqlalchemy import Engine, inspect
from sqlmodel import Session, create_engine

_engine: Engine | None = None
_BASELINE_TABLES = frozenset(
    {
        "product",
        "psg_document",
        "psg_version",
        "be_requirement",
        "query_log",
    }
)
_CURRENT_TABLES = _BASELINE_TABLES | frozenset({"chat_session", "chat_message"})
_BASELINE_REVISION = "0001_initial_schema"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _has_complete_legacy_schema() -> bool:
    """Detect pre-Alembic DBs that already have the baseline tables."""
    engine = get_engine()
    tables = set(inspect(engine).get_table_names())
    if not tables >= _BASELINE_TABLES:
        return False
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision() is None


def _has_complete_current_schema() -> bool:
    """Detect DBs created from current SQLModel metadata before Alembic stamping."""
    engine = get_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not tables >= _CURRENT_TABLES:
        return False
    query_columns = {c["name"] for c in inspector.get_columns("query_log")}
    if not {"session_id", "turn_id", "status", "route_json"} <= query_columns:
        return False
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision() is None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        s.ensure_dirs()
        url = f"sqlite:///{s.sqlite_path.as_posix()}"
        _engine = create_engine(url, echo=False)
    return _engine


def init_db() -> None:
    """Apply schema migrations for the active SQLite database."""
    from alembic import command
    from alembic.config import Config

    from regwatch.store import models  # noqa: F401  (registers tables)

    root = _repo_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{get_settings().sqlite_path.as_posix()}")
    if _has_complete_current_schema():
        command.stamp(cfg, "head")
        return
    if _has_complete_legacy_schema():
        command.stamp(cfg, _BASELINE_REVISION)
        command.upgrade(cfg, "head")
        return
    command.upgrade(cfg, "head")


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
