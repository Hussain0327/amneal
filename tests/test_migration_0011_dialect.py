"""Migration 0011 (ensure_rls event trigger) must be dialect-guarded.

0011 creates a Postgres ``rls_auto_enable()`` function + ``ensure_rls`` event
trigger so tables created AFTER boot still get deny-all RLS. Event triggers are
Postgres-only DDL: replaying 0011 on SQLite (``_init_sqlite``'s
``command.upgrade``) must execute NOTHING, and on Postgres it must emit the
function + trigger DDL. The mock-based tests assert that dialect split without a
live DB (mirrors tests/test_migration_0003_dialect.py); the integration test
(opt-in via TEST_DATABASE_URL) proves the trigger actually RLSes a NEW table.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0011_ensure_rls_event_trigger.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load_migration()


class _FakeBind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = mock.Mock()
        self.dialect.name = dialect_name


def _run(fn_name: str, dialect_name: str) -> list[Any]:
    """Run upgrade()/downgrade() against a fake op for the dialect; record execute()."""
    executed: list[Any] = []
    with (
        mock.patch.object(mig.op, "get_bind", return_value=_FakeBind(dialect_name)),
        mock.patch.object(mig.op, "execute", side_effect=lambda sql, *a, **k: executed.append(sql)),
    ):
        getattr(mig, fn_name)()
    return executed


def _mentions_event_trigger(stmts: list[Any]) -> bool:
    text = " ".join(str(s) for s in stmts).lower()
    return "event trigger" in text and "ensure_rls" in text


def test_upgrade_noop_on_sqlite() -> None:
    # SQLite has no event triggers: the upgrade must execute nothing at all.
    assert _run("upgrade", "sqlite") == []


def test_downgrade_noop_on_sqlite() -> None:
    assert _run("downgrade", "sqlite") == []


def test_upgrade_creates_function_and_trigger_on_postgres() -> None:
    stmts = _run("upgrade", "postgresql")
    joined = " ".join(str(s) for s in stmts).lower()
    assert "create or replace function" in joined
    assert "rls_auto_enable" in joined
    assert "enable row level security" in joined
    assert _mentions_event_trigger(stmts), "ensure_rls event trigger not created on Postgres"
    # Defense-in-depth: every table-creating tag must be covered, else a
    # CREATE TABLE AS / SELECT INTO table comes up un-RLSed between boot sweeps.
    for tag in ("create table", "create table as", "select into"):
        assert tag in joined, f"ensure_rls trigger does not cover the {tag!r} command tag"


def test_downgrade_drops_function_and_trigger_on_postgres() -> None:
    stmts = _run("downgrade", "postgresql")
    joined = " ".join(str(s) for s in stmts).lower()
    assert "drop event trigger if exists ensure_rls" in joined
    assert "drop function if exists" in joined and "rls_auto_enable" in joined


def test_rls_event_trigger_sql_is_idempotent_shape() -> None:
    """The importable DDL (reused by scripts/migrate_to_supabase.py) drops the
    trigger before recreating it, so applying it twice never errors."""
    stmts = mig.rls_event_trigger_sql()
    joined = " ".join(stmts).lower()
    assert "create or replace function" in joined  # function is replace-safe
    assert "drop event trigger if exists ensure_rls" in joined  # trigger is drop-then-create
    assert joined.index("drop event trigger") < joined.index("create event trigger")


# --------------------------------------------------------------------------
# Real-Postgres integration: the trigger must actually RLS a NEW table.
# Opt-in (TEST_DATABASE_URL); skipped under SQLite-only CI.
# --------------------------------------------------------------------------

TEST_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def test_event_trigger_auto_enables_rls_on_new_table() -> None:
    import sqlalchemy as sa

    # Bare postgresql:// URLs default SQLAlchemy to the absent psycopg2
    # driver; force psycopg v3 the same way config.settings normalizes.
    url = TEST_DATABASE_URL
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
            for stmt in mig.rls_event_trigger_sql():
                conn.execute(sa.text(stmt))
        # A table created AFTER the trigger exists must come up RLS-enabled
        # WITHOUT any explicit ALTER from us.
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE public.rls_probe (id int)"))
        with engine.connect() as conn:
            relrowsecurity = conn.execute(
                sa.text(
                    "SELECT c.relrowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname='public' AND c.relname='rls_probe'"
                )
            ).scalar()
        assert relrowsecurity is True, "ensure_rls did not enable RLS on the new table"
        # Reversible: after downgrade the trigger is gone, new tables are NOT RLSed.
        with engine.begin() as conn:
            conn.execute(sa.text("DROP EVENT TRIGGER IF EXISTS ensure_rls"))
            conn.execute(sa.text("DROP FUNCTION IF EXISTS public.rls_auto_enable()"))
            conn.execute(sa.text("CREATE TABLE public.rls_probe2 (id int)"))
        with engine.connect() as conn:
            after = conn.execute(
                sa.text(
                    "SELECT c.relrowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname='public' AND c.relname='rls_probe2'"
                )
            ).scalar()
        assert after is False, "RLS still auto-enabled after dropping the trigger"
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE IF EXISTS public.rls_probe"))
            conn.execute(sa.text("DROP TABLE IF EXISTS public.rls_probe2"))
            conn.execute(sa.text("DROP EVENT TRIGGER IF EXISTS ensure_rls"))
            conn.execute(sa.text("DROP FUNCTION IF EXISTS public.rls_auto_enable()"))
        engine.dispose()
