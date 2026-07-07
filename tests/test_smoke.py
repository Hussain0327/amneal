"""Phase 0 smoke tests: imports, DB boots, providers wire up.

DoD: `uv run pytest` is green on these.
"""

from __future__ import annotations

import importlib

import pytest
from sqlmodel import select


def test_top_level_imports() -> None:
    for mod in (
        "regwatch",
        "regwatch.common.logging",
        "regwatch.common.audit",
        "regwatch.common.text_normalize",
        "regwatch.store.db",
        "regwatch.store.models",
        "regwatch.store.vector_store",
        "regwatch.process.embedder",
        "regwatch.generate.llm",
        "regwatch.cli",
        "config.settings",
    ):
        importlib.import_module(mod)


def test_settings_load() -> None:
    from config.settings import get_settings

    s = get_settings()
    assert s.embedding_provider == "echo"
    assert s.llm_provider == "echo"
    # Assert the refusal contract (declines + won't guess), not exact phrasing,
    # so warming the copy doesn't break this smoke test.
    assert s.refusal_text
    assert "find this" in s.refusal_text.lower()
    assert "won't guess" in s.refusal_text.lower()


def test_database_url_defaults_to_none_sqlite_mode() -> None:
    from config.settings import get_settings

    assert get_settings().database_url is None


def test_database_url_normalized_to_psycopg_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    cases = {
        "postgresql://u:p@h:5432/db": "postgresql+psycopg://u:p@h:5432/db",
        "postgres://u:p@h:5432/db": "postgresql+psycopg://u:p@h:5432/db",
        "postgresql+psycopg://u:p@h:5432/db": "postgresql+psycopg://u:p@h:5432/db",
    }
    for raw, expected in cases.items():
        monkeypatch.setenv("DATABASE_URL", raw)
        cs.get_settings.cache_clear()
        assert cs.get_settings().database_url == expected
    monkeypatch.setenv("DATABASE_URL", "   ")
    cs.get_settings.cache_clear()
    assert cs.get_settings().database_url is None
    monkeypatch.setenv("DATABASE_URL", "")
    cs.get_settings.cache_clear()
    assert cs.get_settings().database_url is None


def test_engine_dialect_branches_on_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """No server needed: create_engine is lazy, so this proves dispatch only."""
    import config.settings as cs

    from regwatch.store import db as db_module

    assert db_module.get_engine().dialect.name == "sqlite"

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:5499/regwatch_test")
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()
    engine = db_module.get_engine()
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg"
    db_module.reset_for_tests()


def test_init_db_sqlite_creates_chat_session_composite_index() -> None:
    from sqlalchemy import inspect

    from regwatch.store.db import get_engine, init_db

    init_db()
    # Migration 0007 creates the index through alembic's own engine; dispose
    # the cached engine's pool so PRAGMA-based introspection below doesn't
    # read a pooled connection's pre-migration schema cache.
    get_engine().dispose()
    names = {ix["name"] for ix in inspect(get_engine()).get_indexes("chat_session")}
    assert "ix_chat_session_user_id_updated_at" in names
    # Idempotent (CREATE INDEX IF NOT EXISTS in 0007): a second boot must not fail.
    init_db()


def test_json_columns_get_jsonb_variant_on_postgres() -> None:
    from sqlalchemy.dialects import postgresql, sqlite

    from regwatch.store.models import QueryLog

    column = QueryLog.__table__.c.route_json  # type: ignore[attr-defined]
    assert (
        column.type.compile(dialect=postgresql.dialect()) == "JSONB"  # type: ignore[no-untyped-call]
    )
    assert column.type.compile(dialect=sqlite.dialect()) == "JSON"


def test_db_boots_and_round_trips() -> None:
    from sqlalchemy import inspect, text

    from regwatch.store.db import get_engine, init_db, session_scope
    from regwatch.store.models import Product

    init_db()
    assert "alembic_version" in inspect(get_engine()).get_table_names()
    with get_engine().connect() as conn:
        assert (
            conn.execute(text("select version_num from alembic_version")).scalar_one()
            == "0013_whitepaper_runs"
        )
    with session_scope() as s:
        s.add(
            Product(
                active_ingredient="Albuterol Sulfate",
                normalized_name="albuterol",
                dosage_form="Inhalation Aerosol, Metered",
                route="Inhalation",
                rld_name="ProAir HFA",
                rld_application_number="021457",
                company_status="pipeline",
                source="manual",
                on_watchlist=True,
            )
        )

    with session_scope() as s2:
        stmt = select(Product).where(Product.normalized_name == "albuterol")
        rows = list(s2.scalars(stmt))
        assert len(rows) == 1
        assert rows[0].rld_application_number == "021457"


def test_init_db_stamps_complete_legacy_schema_without_version_table() -> None:
    from sqlalchemy import inspect, text
    from sqlmodel import SQLModel

    from regwatch.store import models  # noqa: F401  (register tables)
    from regwatch.store.db import get_engine, init_db

    SQLModel.metadata.create_all(get_engine())
    assert "alembic_version" not in inspect(get_engine()).get_table_names()

    init_db()

    with get_engine().connect() as conn:
        assert (
            conn.execute(text("select version_num from alembic_version")).scalar_one()
            == "0013_whitepaper_runs"
        )


def test_init_db_stamps_complete_legacy_schema_with_empty_version_table() -> None:
    from sqlalchemy import inspect, text
    from sqlmodel import SQLModel

    from regwatch.store import models  # noqa: F401  (register tables)
    from regwatch.store.db import get_engine, init_db

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text("create table alembic_version " "(version_num varchar(32) not null primary key)")
        )
    assert "alembic_version" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).fetchall() == []

    init_db()

    with engine.connect() as conn:
        assert (
            conn.execute(text("select version_num from alembic_version")).scalar_one()
            == "0013_whitepaper_runs"
        )


def test_chroma_round_trip() -> None:
    from regwatch.process.embedder import get_embedding_provider
    from regwatch.store.vector_store import add_chunks, collection_size, similarity_search

    p = get_embedding_provider()
    texts = ["fasting bioequivalence", "single-dose crossover", "dissolution method 2"]
    vecs = p.embed(texts)
    add_chunks(
        ids=["a", "b", "c"],
        embeddings=vecs,
        documents=texts,
        metadatas=[
            {"doc_id": 1, "page": 1, "normalized_name": "x", "source_url": "u"},
            {"doc_id": 1, "page": 2, "normalized_name": "x", "source_url": "u"},
            {"doc_id": 2, "page": 1, "normalized_name": "y", "source_url": "u"},
        ],
    )
    assert collection_size() == 3
    qv = p.embed(["single-dose crossover"])[0]
    hits = similarity_search(qv, k=3)
    assert hits
    assert hits[0].text == "single-dose crossover"
    assert 0.0 <= hits[0].score <= 1.0


def test_text_normalize() -> None:
    from regwatch.common.text_normalize import canonical_name, is_combo, stripped_name

    assert canonical_name("Albuterol Sulfate") == "albuterol sulfate"
    assert stripped_name("Albuterol Sulfate") == "albuterol"
    a = canonical_name("Hydrocodone Bitartrate; Acetaminophen")
    b = canonical_name("Acetaminophen and Hydrocodone Bitartrate")
    assert a == b
    assert is_combo(a)
    assert stripped_name("Hydrocodone Bitartrate; Acetaminophen") == "acetaminophen; hydrocodone"


def test_llm_provider_factory_echo() -> None:
    from regwatch.generate.llm import LLMMessage, get_llm_provider

    p = get_llm_provider("echo")
    out = p.complete([LLMMessage(role="user", content="hello")])
    assert "hello" in out.text


def test_llm_provider_openai_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    # Use empty string instead of delenv: pydantic-settings would otherwise
    # fall back to the host's .env which may have a real key.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    import config.settings as cs

    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    from regwatch.generate.llm import get_llm_provider

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_llm_provider()


def test_cli_status_runs() -> None:
    from typer.testing import CliRunner

    from regwatch.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "echo" in result.output


def test_enforce_sslmode_adds_require_for_remote_host() -> None:
    """Public-internet Supabase pooler → sslmode=require is injected."""
    from regwatch.store.db import _enforce_sslmode

    url = _enforce_sslmode(
        "postgresql+psycopg://postgres.ref:pw@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
    )
    assert dict(url.query)["sslmode"] == "require"
    # The password survives (URL object, not the ***-masked string).
    assert url.password == "pw"


def test_enforce_sslmode_leaves_local_hosts_untouched() -> None:
    """CI's Postgres service container + local docker-compose Postgres get no sslmode."""
    from regwatch.store.db import _enforce_sslmode

    for host in ("localhost", "127.0.0.1", "[::1]"):
        url = _enforce_sslmode(f"postgresql+psycopg://postgres:postgres@{host}:5432/postgres")
        assert "sslmode" not in url.query, host
    # sqlite passes through verbatim too.
    assert "sslmode" not in _enforce_sslmode("sqlite:////tmp/x.db").query


def test_enforce_sslmode_respects_explicit_sslmode() -> None:
    """An operator-set sslmode (even on a remote host) is never overridden."""
    from regwatch.store.db import _enforce_sslmode

    keep = _enforce_sslmode(
        "postgresql+psycopg://u:p@aws-1-us-east-1.pooler.supabase.com:5432/postgres?sslmode=verify-full"
    )
    assert dict(keep.query)["sslmode"] == "verify-full"
    disable = _enforce_sslmode("postgresql+psycopg://u:p@some.remote.host:5432/db?sslmode=disable")
    assert dict(disable.query)["sslmode"] == "disable"


def test_migration_connect_args_bounds_lock_timeout_only() -> None:
    """The release migration connection gets a lock_timeout (so a contended
    migration self-cancels) but NOT a statement_timeout (a long index build must
    be allowed to finish) — the deliberate divergence from the app engine."""
    from config.settings import Settings

    from regwatch.store.db import _migration_connect_args

    args = _migration_connect_args(Settings(db_lock_timeout="10s"))
    assert args == {"options": "-c lock_timeout=10s"}
    # The whole point: no statement_timeout / idle_in_transaction on migrations.
    assert "statement_timeout" not in args["options"]
    assert "idle_in_transaction_session_timeout" not in args["options"]
    # Disabled when unset/0.
    assert _migration_connect_args(Settings(db_lock_timeout="0")) == {}
    assert _migration_connect_args(Settings(db_lock_timeout="")) == {}
