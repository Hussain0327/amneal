"""Unit tests for scripts/migrate_to_supabase.py — no live Postgres needed.

The script is loaded by file path (scripts/ is not a package). Engines are
SQLite stand-ins under tmp_path; the Postgres-only behavior (sequence resets)
is tested at the SQL-generation level. Covered: FK-dependency table ordering,
chunked executemany batching with id preservation, refuse-on-nonempty /
--truncate semantics, URL normalization, and count verification.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlmodel import SQLModel


def _load_script() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_to_supabase.py"
    spec = importlib.util.spec_from_file_location("migrate_to_supabase", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load_script()


def _engine(tmp_path: Path, name: str) -> sa.Engine:
    eng = sa.create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    SQLModel.metadata.create_all(eng)
    return eng


def _product_row(i: int) -> dict[str, Any]:
    return {
        "id": i,
        "active_ingredient": f"ingredient-{i}",
        "normalized_name": f"name-{i}",
        "dosage_form": None,
        "route": None,
        "rld_name": None,
        "rld_application_number": None,
        "company_status": None,
        "source": "manual",
        "source_url": None,
        "on_watchlist": True,
        "added_at": datetime.now(UTC),
    }


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


def test_normalize_database_url_variants() -> None:
    assert (
        mig.normalize_database_url("postgresql://u:p@host:5432/db")
        == "postgresql+psycopg://u:p@host:5432/db"
    )
    assert (
        mig.normalize_database_url("postgres://u:p@host:5432/db")
        == "postgresql+psycopg://u:p@host:5432/db"
    )
    assert (
        mig.normalize_database_url("postgresql+psycopg://u:p@host/db")
        == "postgresql+psycopg://u:p@host/db"
    )


def test_normalize_database_url_refuses_non_postgres() -> None:
    with pytest.raises(mig.MigrationError):
        mig.normalize_database_url("sqlite:///data/regwatch.db")
    with pytest.raises(mig.MigrationError):
        mig.normalize_database_url("mysql://u:p@host/db")


# ---------------------------------------------------------------------------
# Table ordering
# ---------------------------------------------------------------------------


def test_ordered_tables_respects_fk_dependencies() -> None:
    names = [t.name for t in mig.ordered_tables()]
    # Parents strictly before children.
    assert names.index("psg_document") < names.index("psg_version")
    assert names.index("psg_document") < names.index("be_requirement")
    assert names.index("psg_version") < names.index("be_requirement")
    assert names.index("user") < names.index("auth_session")
    assert names.index("chat_session") < names.index("chat_message")


def test_ordered_tables_covers_all_models_and_excludes_special() -> None:
    names = {t.name for t in mig.ordered_tables()}
    expected = {
        "product",
        "psg_document",
        "psg_version",
        "be_requirement",
        "query_log",
        "user",
        "auth_session",
        "chat_session",
        "chat_message",
        "ob_product",
        "ob_patent",
        "ob_exclusivity",
        "spl_document",
    }
    assert expected <= names
    # `chunk` is rebuilt from Chroma and `alembic_version` is stamped, never copied.
    assert "chunk" not in names
    assert "alembic_version" not in names


# ---------------------------------------------------------------------------
# Chunked copy
# ---------------------------------------------------------------------------


def test_copy_table_batches_and_preserves_ids(tmp_path: Path) -> None:
    src = _engine(tmp_path, "src.db")
    dst = _engine(tmp_path, "dst.db")
    product = SQLModel.metadata.tables["product"]

    rows = [_product_row(i) for i in range(1, 2501)]
    with src.begin() as conn:
        conn.execute(product.insert(), rows)

    batches: list[int] = []

    @event.listens_for(dst, "before_cursor_execute")
    def _capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if executemany and statement.lstrip().upper().startswith("INSERT INTO PRODUCT"):
            batches.append(len(parameters))

    copied = mig.copy_table(src, dst, product, batch_size=1000)

    assert copied == 2500
    assert batches == [1000, 1000, 500]
    assert mig.count_rows(dst, product) == 2500
    # Ids are preserved verbatim (explicit-pk insert), content intact.
    with dst.connect() as conn:
        got = conn.execute(
            sa.select(product.c.id, product.c.normalized_name).where(
                product.c.id.in_([1, 1337, 2500])
            )
        ).all()
    assert sorted(got) == [(1, "name-1"), (1337, "name-1337"), (2500, "name-2500")]


def test_copy_table_empty_source(tmp_path: Path) -> None:
    src = _engine(tmp_path, "src.db")
    dst = _engine(tmp_path, "dst.db")
    product = SQLModel.metadata.tables["product"]
    assert mig.copy_table(src, dst, product, batch_size=1000) == 0
    assert mig.count_rows(dst, product) == 0


# ---------------------------------------------------------------------------
# Refuse-on-nonempty / --truncate
# ---------------------------------------------------------------------------


def test_prepare_target_refuses_nonempty(tmp_path: Path) -> None:
    dst = _engine(tmp_path, "dst.db")
    product = SQLModel.metadata.tables["product"]
    with dst.begin() as conn:
        conn.execute(product.insert(), [_product_row(1)])

    with pytest.raises(mig.MigrationError, match="--truncate") as excinfo:
        mig.prepare_target(dst, mig.ordered_tables(), truncate=False)
    assert "product" in str(excinfo.value)


def test_prepare_target_truncate_wipes_rows(tmp_path: Path) -> None:
    dst = _engine(tmp_path, "dst.db")
    product = SQLModel.metadata.tables["product"]
    with dst.begin() as conn:
        conn.execute(product.insert(), [_product_row(1), _product_row(2)])

    mig.prepare_target(dst, mig.ordered_tables(), truncate=True)
    assert mig.count_rows(dst, product) == 0
    # And an empty target passes without --truncate.
    mig.prepare_target(dst, mig.ordered_tables(), truncate=False)


def test_prepare_target_detects_and_truncates_chunk_table(tmp_path: Path) -> None:
    # NOTE: once regwatch.store.pgvector_store has been imported anywhere in the
    # process (pytest collection does), its Chunk SQLModel is registered and
    # create_all has already built a `chunk` table — hence IF NOT EXISTS.
    dst = _engine(tmp_path, "dst.db")
    with dst.begin() as conn:
        conn.execute(sa.text('CREATE TABLE IF NOT EXISTS "chunk" (id TEXT PRIMARY KEY, text TEXT)'))
        conn.execute(sa.text("INSERT INTO \"chunk\" (id, text) VALUES ('c1', 'hello')"))

    nonempty = mig.nonempty_target_tables(dst, mig.ordered_tables())
    assert nonempty == ["chunk"]
    with pytest.raises(mig.MigrationError, match="chunk"):
        mig.prepare_target(dst, mig.ordered_tables(), truncate=False)

    mig.prepare_target(dst, mig.ordered_tables(), truncate=True)
    with dst.connect() as conn:
        assert conn.execute(sa.text('SELECT COUNT(*) FROM "chunk"')).scalar_one() == 0


def test_drop_unstamped_refuses_without_truncate(tmp_path: Path) -> None:
    # Tables exist but no alembic_version stamp (e.g. a reused rehearsal DB
    # that the pg test suite booted and partially tore down): without
    # --truncate the script must refuse with the same guidance init_db gives.
    dst = _engine(tmp_path, "dst.db")
    with pytest.raises(mig.MigrationError, match="--truncate"):
        mig.drop_unstamped_target_tables(dst, truncate=False)


def test_drop_unstamped_noop_when_stamped_or_fresh(tmp_path: Path) -> None:
    # Stamped target: init_db owns head verification — nothing to recover.
    dst = _engine(tmp_path, "stamped.db")
    with dst.begin() as conn:
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)"))
    assert mig.drop_unstamped_target_tables(dst, truncate=False) is False
    assert mig.drop_unstamped_target_tables(dst, truncate=True) is False
    # Fresh target (no regwatch tables at all): also a no-op.
    fresh = sa.create_engine(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    assert mig.drop_unstamped_target_tables(fresh, truncate=True) is False


def _alembic_cfg(db_path: Path) -> Any:
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_upgrade_source_snapshot_upgrades_behind_head(tmp_path: Path) -> None:
    # Build a source stamped one revision behind head (the 2026-06-11 prod
    # snapshot shape: 0005, missing ob_exclusivity.appl_type from 0006).
    from alembic import command

    src = tmp_path / "behind.db"
    command.upgrade(_alembic_cfg(src), "0005_whitepaper_sources")

    upgraded = mig.upgrade_source_snapshot(src)
    assert upgraded != src  # private temp copy; snapshot untouched
    eng = sa.create_engine(f"sqlite:///{upgraded.as_posix()}")
    cols = {c["name"] for c in sa.inspect(eng).get_columns("ob_exclusivity")}
    assert "appl_type" in cols
    # Snapshot itself was NOT upgraded.
    src_eng = sa.create_engine(f"sqlite:///{src.as_posix()}")
    src_cols = {c["name"] for c in sa.inspect(src_eng).get_columns("ob_exclusivity")}
    assert "appl_type" not in src_cols


def test_upgrade_source_snapshot_noop_at_head(tmp_path: Path) -> None:
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    src = tmp_path / "at-head.db"
    cfg = _alembic_cfg(src)
    command.upgrade(cfg, "head")
    head = ScriptDirectory.from_config(cfg).get_current_head()

    upgraded = mig.upgrade_source_snapshot(src)
    eng = sa.create_engine(f"sqlite:///{upgraded.as_posix()}")
    with eng.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == head


def test_upgrade_source_snapshot_refuses_unstamped(tmp_path: Path) -> None:
    _engine(tmp_path, "unstamped.db")  # current tables, no alembic_version
    with pytest.raises(mig.MigrationError, match="alembic_version"):
        mig.upgrade_source_snapshot(tmp_path / "unstamped.db")


# ---------------------------------------------------------------------------
# Sequence reset SQL (Postgres-only behavior, tested as generated SQL)
# ---------------------------------------------------------------------------


def test_sequence_reset_statements_cover_int_pks_only() -> None:
    stmts = mig.sequence_reset_statements(mig.ordered_tables())
    joined = "\n".join(stmts)
    # Integer-autoincrement pks get a setval; reserved names are quoted.
    assert "pg_get_serial_sequence('\"product\"', 'id')" in joined
    assert "pg_get_serial_sequence('\"user\"', 'id')" in joined
    assert "pg_get_serial_sequence('\"query_log\"', 'id')" in joined
    # String-pk tables have no sequence to reset.
    assert "chat_session" not in joined
    assert "chat_message" not in joined
    # setval(max+1, false): next nextval() returns max+1 (1 on an empty table).
    assert all("COALESCE" in s and "+ 1, false" in s for s in stmts)


def test_reset_sequences_noop_on_sqlite(tmp_path: Path) -> None:
    dst = _engine(tmp_path, "dst.db")
    assert mig.reset_sequences(dst, mig.ordered_tables()) == 0


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_verify_migration_passes_on_equal_counts(tmp_path: Path) -> None:
    src = _engine(tmp_path, "src.db")
    dst = _engine(tmp_path, "dst.db")
    product = SQLModel.metadata.tables["product"]
    rows = [_product_row(i) for i in range(1, 4)]
    with src.begin() as conn:
        conn.execute(product.insert(), rows)
    mig.copy_table(src, dst, product)

    failures = mig.verify_migration(
        src, dst, mig.ordered_tables(), chunk_source_count=5, chunk_target_count=5
    )
    assert failures == []


def test_verify_migration_flags_any_mismatch(tmp_path: Path) -> None:
    src = _engine(tmp_path, "src.db")
    dst = _engine(tmp_path, "dst.db")
    product = SQLModel.metadata.tables["product"]
    with src.begin() as conn:
        conn.execute(product.insert(), [_product_row(1), _product_row(2)])
    mig.copy_table(src, dst, product)
    with dst.begin() as conn:  # simulate a silent partial copy
        conn.execute(product.delete().where(product.c.id == 2))

    failures = mig.verify_migration(
        src, dst, mig.ordered_tables(), chunk_source_count=10, chunk_target_count=9
    )
    assert any(f.startswith("product:") for f in failures)
    assert any(f.startswith("chunk:") for f in failures)
