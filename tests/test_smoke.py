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
    assert s.refusal_text.startswith("I can't find this")


def test_db_boots_and_round_trips() -> None:
    from sqlalchemy import inspect, text

    from regwatch.store.db import get_engine, init_db, session_scope
    from regwatch.store.models import Product

    init_db()
    assert "alembic_version" in inspect(get_engine()).get_table_names()
    with get_engine().connect() as conn:
        assert (
            conn.execute(text("select version_num from alembic_version")).scalar_one()
            == "0002_chat_sessions"
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
            == "0002_chat_sessions"
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
            == "0002_chat_sessions"
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
