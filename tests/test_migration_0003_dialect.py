"""Migration 0003 backfill must be dialect-guarded.

0003 backfills psg_document.appl_no from source_url using SQLite's
instr()/substr(). Those functions do not exist in Postgres, so replaying 0003
on a Postgres DB stamped behind head (the Fly release_command incremental
`alembic upgrade head`) would raise UndefinedFunction. The backfill UPDATE must
run ONLY under SQLite; on Postgres it is intentionally skipped (column stays
NULL). The portable schema ops (add_column, create_index) must run on BOTH.

The module is loaded by file path because migrations/versions is not an
importable package (mirrors the load-by-path pattern in test_migrate_script).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0003_psg_document_appl_no.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load_migration()


class _FakeBind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = mock.Mock()
        self.dialect.name = dialect_name


def _is_backfill_update(sql: str) -> bool:
    text = " ".join(sql.split()).lower()
    return "update psg_document" in text and "instr(" in text and "substr(" in text


def _run_upgrade(dialect_name: str) -> dict[str, list[Any]]:
    """Run upgrade() against a fake op for the given dialect; record calls."""
    calls: dict[str, list[Any]] = {"execute": [], "add_column": [], "create_index": []}
    with (
        mock.patch.object(mig.op, "get_bind", return_value=_FakeBind(dialect_name)),
        mock.patch.object(
            mig.op, "execute", side_effect=lambda sql, *a, **k: calls["execute"].append(sql)
        ),
        mock.patch.object(
            mig.op,
            "add_column",
            side_effect=lambda *a, **k: calls["add_column"].append((a, k)),
        ),
        mock.patch.object(
            mig.op,
            "create_index",
            side_effect=lambda *a, **k: calls["create_index"].append((a, k)),
        ),
    ):
        mig.upgrade()
    return calls


def test_backfill_skipped_on_postgres() -> None:
    calls = _run_upgrade("postgresql")
    # The instr()/substr() backfill UPDATE must NOT run on Postgres.
    assert not any(
        isinstance(sql, str) and _is_backfill_update(sql) for sql in calls["execute"]
    ), "0003 backfill UPDATE leaked onto Postgres (instr()/substr() would raise)"
    # Portable schema ops still run on Postgres.
    assert len(calls["add_column"]) == 1
    assert len(calls["create_index"]) == 1


def test_backfill_runs_on_sqlite() -> None:
    calls = _run_upgrade("sqlite")
    assert any(
        isinstance(sql, str) and _is_backfill_update(sql) for sql in calls["execute"]
    ), "0003 backfill UPDATE must run under SQLite"
    # Portable schema ops also run on SQLite.
    assert len(calls["add_column"]) == 1
    assert len(calls["create_index"]) == 1
