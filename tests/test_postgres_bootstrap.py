"""Postgres bootstrap integration tests (K2/K4).

These run ONLY when TEST_DATABASE_URL points at a disposable Postgres with the
pgvector extension available (the integration agent provides it), e.g.:

    docker run -d --name regwatch-mig-pg -e POSTGRES_PASSWORD=pw \
        -p 127.0.0.1:5499:5432 pgvector/pgvector:pg17
    TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:5499/postgres \
        uv run pytest tests/test_postgres_bootstrap.py

The target's public schema is DROPPED between tests — never point this at a
real database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import ModuleType

import pytest
from sqlalchemy import inspect, text

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set (postgres integration tests are opt-in)",
)


@pytest.fixture()
def pg_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Point the app at the test Postgres and wipe its public schema."""
    import config.settings as cs

    from regwatch.store import db as db_module

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # init_db asserts provider dim == vector(1536) in Postgres mode (K6), so
    # the bootstrap tests need the 1536-dim provider. No API key is required —
    # the assert reads `.dim` without instantiating a client.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()
    engine = db_module.get_engine()
    assert engine.dialect.name == "postgresql", "TEST_DATABASE_URL must be a postgres URL"
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield db_module
    db_module.reset_for_tests()


def _stamped_revision(db_module: ModuleType) -> str | None:
    with db_module.get_engine().connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return row


def test_fresh_bootstrap_creates_schema_and_stamps_head(pg_db: ModuleType) -> None:
    pg_db.init_db()
    engine = pg_db.get_engine()
    tables = set(inspect(engine).get_table_names())
    expected = {
        "product",
        "psg_document",
        "psg_version",
        "be_requirement",
        "query_log",
        "chat_session",
        "chat_message",
        "user",
        "auth_session",
        "ob_product",
        "ob_patent",
        "ob_exclusivity",
        "spl_document",
        "chunk",
        "alembic_version",
    }
    assert expected <= tables
    head = pg_db._head_revision(pg_db._alembic_config())
    assert _stamped_revision(pg_db) == head


def test_bootstrap_enables_rls_on_every_public_table(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
        ).all()
        policies = conn.execute(
            text("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        ).scalar()
    assert rows, "expected public tables after bootstrap"
    without_rls = sorted(name for name, enabled in rows if not enabled)
    assert not without_rls, f"tables missing RLS: {without_rls}"
    # Deny-all: RLS enabled with NO policies (Data-API roles get nothing).
    assert policies == 0


def test_bootstrap_creates_chunk_table_and_indexes(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        embedding_type = conn.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = 'public.chunk'::regclass AND a.attname = 'embedding'"
            )
        ).scalar()
        index_defs = {
            name: ddl
            for name, ddl in conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunk'")
            )
        }
    assert embedding_type == "vector(1536)"
    assert "hnsw" in index_defs["ix_chunk_embedding_hnsw"]
    # Must match the btree indexes the pgvector_store.Chunk model declares —
    # both bootstrap paths produce the same schema regardless of import order.
    assert {
        "ix_chunk_normalized_name",
        "ix_chunk_doc_id",
        "ix_chunk_version_id",
        "ix_chunk_appl_no",
    } <= set(index_defs)


def test_bootstrap_creates_chat_session_composite_index(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'chat_session'")
            )
        }
    assert "ix_chat_session_user_id_updated_at" in names


def test_json_columns_are_jsonb_on_postgres(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        data_type = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'query_log' "
                "AND column_name = 'route_json'"
            )
        ).scalar()
    assert data_type == "jsonb"


def test_round_trip_through_session_scope(pg_db: ModuleType) -> None:
    from sqlmodel import select

    from regwatch.store.models import Product

    pg_db.init_db()
    with pg_db.session_scope() as s:
        s.add(
            Product(
                active_ingredient="Albuterol Sulfate",
                normalized_name="albuterol",
                source="manual",
            )
        )
    with pg_db.session_scope() as s:
        rows = list(s.scalars(select(Product).where(Product.normalized_name == "albuterol")))
    assert len(rows) == 1


def test_second_boot_is_idempotent(pg_db: ModuleType) -> None:
    pg_db.init_db()
    pg_db.init_db()  # stamped at head -> verify + idempotent ensures, no error
    head = pg_db._head_revision(pg_db._alembic_config())
    assert _stamped_revision(pg_db) == head


def test_refuses_to_start_on_revision_mismatch(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '0000_bogus'"))
    # init_db is memoized per process; reset to simulate a fresh process boot
    # against the tampered DB — the state the boot refusal actually guards.
    pg_db.reset_for_tests()
    with pytest.raises(RuntimeError, match="stamped at alembic revision"):
        pg_db.init_db()


def test_alembic_upgrade_head_heals_0007_stamped_postgres(pg_db: ModuleType) -> None:
    """H3 existing-Postgres path: a 0007-stamped DB reaches head via the
    documented operator one-liner (`alembic upgrade head`, DEPLOY.md §3/§6.3)
    after this build's boot refusal — 0008's batch ops are PG-compatible."""
    from alembic import command

    pg_db.init_db()  # fresh bootstrap: create_all + stamp head
    cfg = pg_db._alembic_config()
    # Rewind to the 0007 shape (0008's downgrade runs fine on Postgres),
    # leaving the stamp at 0007 — the state a parallel cutover produces.
    command.downgrade(cfg, "0007_chat_session_user_updated")
    inspector = inspect(pg_db.get_engine())
    assert "answer_feedback" not in inspector.get_table_names()
    assert "input_tokens" not in {c["name"] for c in inspector.get_columns("query_log")}
    assert _stamped_revision(pg_db) == "0007_chat_session_user_updated"

    # Booting this build against it refuses (by design) ... reset the per-
    # process init_db memoization first so this models an actual fresh boot.
    pg_db.reset_for_tests()
    with pytest.raises(RuntimeError, match="stamped at alembic revision"):
        pg_db.init_db()

    # ... and the documented one-liner heals it.
    command.upgrade(cfg, "head")
    pg_db.reset_for_tests()
    inspector = inspect(pg_db.get_engine())
    cols = {c["name"] for c in inspector.get_columns("query_log")}
    assert {"input_tokens", "output_tokens", "cost_usd"} <= cols
    assert "answer_feedback" in inspector.get_table_names()
    pg_db.init_db()  # boots clean at head
    head = pg_db._head_revision(pg_db._alembic_config())
    assert _stamped_revision(pg_db) == head


def test_refuses_to_start_on_unstamped_nonempty_database(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    # init_db is memoized per process; reset to simulate a fresh process boot
    # against the now-unstamped DB — the state the boot refusal actually guards.
    pg_db.reset_for_tests()
    with pytest.raises(RuntimeError, match="no alembic_version stamp"):
        pg_db.init_db()


def test_init_db_fails_fast_on_embedding_dim_mismatch(
    pg_db: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K6: a wrong-dim provider must refuse at startup, not 500 at first use."""
    import config.settings as cs

    monkeypatch.setenv("EMBEDDING_PROVIDER", "local-bge-small")  # 384-dim
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    with pytest.raises(RuntimeError, match="384-dim"):
        pg_db.init_db()
