"""One-shot data migration: SQLite + Chroma snapshots -> Postgres (Supabase) + pgvector.

Copies the entire structured store (every SQLModel table, ids preserved) from a
SQLite snapshot into the Postgres database at --database-url, re-embeds every
Chroma chunk with the OpenAI provider (text-embedding-3-small, 1536 dims), and
inserts them into the pgvector ``chunk`` table. The target schema is
bootstrapped through the same code path the API uses (store/db.py:
``init_db()`` -> create_all + RLS + alembic stamp), so a migrated database is
indistinguishable from a fresh boot.

Usage::

    uv run python scripts/migrate_to_supabase.py \\
        --sqlite /path/to/snapshot/regwatch.db \\
        --chroma /path/to/snapshot/chroma \\
        --database-url 'postgresql://postgres.<ref>:<pw>@<region>.pooler.supabase.com:5432/postgres' \\
        [--skip-embed] [--truncate]

Always point --sqlite/--chroma at SNAPSHOT copies of ``data/`` (e.g.
``cp -R data /tmp/regwatch-snapshot``), never at the live production corpus.

Safety / idempotency:

* Refuses to run against a non-empty target unless ``--truncate`` is passed;
  ``--truncate`` wipes the target application tables (children first) and the
  ``chunk`` table before copying.
* ``--skip-embed`` skips the Chroma -> pgvector step entirely (rehearse the
  relational copy without paying for OpenAI embeddings). The existing Chroma
  vectors are 384-dim (bge-small) and can NOT be reused for the 1536-dim
  ``chunk`` table — a real cutover must re-embed.
* Per-table source-vs-target row counts (and chunk count vs the Chroma count)
  are verified at the end; ANY mismatch exits nonzero. A silent partial copy
  is the worst failure mode.
* NOT resumable. Each table and each 512-row chunk page commits in its own
  transaction (there is no outer transaction), so a mid-run failure (pooler
  blip, OpenAI exhausting retries, OOM) leaves a committed-but-partial target.
  Recovery is a full ``--truncate`` re-run, which re-copies every table AND
  re-embeds the ENTIRE corpus through OpenAI again (the chunk upsert is
  idempotent, but there is no high-water-mark to resume from). Budget for the
  full re-embed cost when planning a cutover, and prefer a low-contention
  window. The end-of-run count verification still ensures a partial copy is
  never silently accepted.
* Postgres sequences are reset via ``setval(max(id)+1)`` after the copy —
  rows arrive with explicit ids, so without this the next INSERT would collide
  (the Postgres equivalent of copying ``sqlite_sequence``).
* If the store layer does not pick up DATABASE_URL (i.e. the engine is not
  Postgres), the script aborts before writing anything — it can never fall
  through to the live SQLite/Chroma paths.
* The SQLite snapshot is read through a private temp copy which is
  alembic-upgraded to head first when the snapshot is stamped behind it (a
  live deployment that has not rebooted since the latest migration). The
  snapshot file itself is never written.

Exit codes: 0 success; 1 verification mismatch; 2 configuration/usage error;
3 refused (non-empty target without --truncate).
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

# Make `regwatch` importable when running as a plain script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlmodel import SQLModel

from regwatch.store import models  # noqa: F401  (registers SQLModel tables)

BATCH_SIZE = 1000  # rows per executemany batch for the relational copy
EMBED_BATCH_SIZE = 512  # texts per OpenAI embedding request (provider max)
CHUNK_TABLE = "chunk"
CHUNK_EMBEDDING_DIM = 1536  # text-embedding-3-small; must match vector(1536) DDL
# Never copied row-by-row from SQLite: `chunk` is rebuilt from Chroma via
# re-embedding (step 3) and `alembic_version` is stamped, not copied (step 5).
EXCLUDED_TABLES = frozenset({CHUNK_TABLE, "alembic_version"})


class MigrationError(RuntimeError):
    """A condition that must abort the migration (config, refusal, mismatch)."""


# --------------------------------------------------------------------------
# Pure helpers (unit-tested without a live Postgres)
# --------------------------------------------------------------------------


def normalize_database_url(url: str) -> str:
    """Normalize a Postgres URL to the SQLAlchemy psycopg v3 driver form.

    ``postgresql://`` / ``postgres://`` -> ``postgresql+psycopg://`` (the K1
    convention). Anything that is not a Postgres URL is refused outright — this
    script must never run against SQLite or any other backend.
    """
    u = url.strip()
    if u.startswith("postgresql+psycopg://"):
        return u
    if u.startswith("postgresql://"):
        return "postgresql+psycopg://" + u[len("postgresql://") :]
    if u.startswith("postgres://"):
        return "postgresql+psycopg://" + u[len("postgres://") :]
    scheme = u.split("://", 1)[0] if "://" in u else u
    raise MigrationError(f"--database-url must be a postgres URL, got scheme {scheme!r}")


def ordered_tables() -> list[sa.Table]:
    """All SQLModel tables in FK-dependency order (parents before children).

    ``MetaData.sorted_tables`` is a topological sort on foreign keys, so a
    chunked copy in this order never violates a FK on the target.
    """
    return [t for t in SQLModel.metadata.sorted_tables if t.name not in EXCLUDED_TABLES]


def existing_tables(engine: Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def count_rows(engine: Engine, table: sa.Table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def nonempty_target_tables(dst_engine: Engine, tables: Sequence[sa.Table]) -> list[str]:
    """Names of target tables that already contain rows (including `chunk`)."""
    present = existing_tables(dst_engine)
    out = [t.name for t in tables if t.name in present and count_rows(dst_engine, t) > 0]
    if CHUNK_TABLE in present:
        with dst_engine.connect() as conn:
            n = int(conn.execute(sa.text(f'SELECT COUNT(*) FROM "{CHUNK_TABLE}"')).scalar_one())
        if n > 0:
            out.append(CHUNK_TABLE)
    return out


def truncate_target(dst_engine: Engine, tables: Sequence[sa.Table]) -> None:
    """Wipe target application tables, children first, plus `chunk`."""
    present = existing_tables(dst_engine)
    with dst_engine.begin() as conn:
        if CHUNK_TABLE in present:
            conn.execute(sa.text(f'DELETE FROM "{CHUNK_TABLE}"'))
        for t in reversed(list(tables)):
            if t.name in present:
                conn.execute(t.delete())


def prepare_target(dst_engine: Engine, tables: Sequence[sa.Table], *, truncate: bool) -> None:
    """Refuse a non-empty target unless --truncate was passed (idempotency rule)."""
    nonempty = nonempty_target_tables(dst_engine, tables)
    if nonempty and not truncate:
        raise MigrationError(
            "target database is not empty "
            f"(tables with rows: {', '.join(sorted(nonempty))}); "
            "pass --truncate to wipe it first"
        )
    if truncate:
        truncate_target(dst_engine, tables)


def upgrade_source_snapshot(sqlite_path: Path) -> Path:
    """Copy the SQLite snapshot to a private temp file and upgrade it to head.

    The source snapshot may be stamped at an older alembic revision than this
    build (e.g. the live deployment has not rebooted since the latest
    migration landed — the 2026-06-11 prod snapshot is at 0005 while head is
    0006). ``copy_table`` SELECTs with current-model columns, so the source
    schema must be at head. The user's snapshot is NEVER written: the upgrade
    runs against a temp copy, which also keeps SQLite WAL/journal side-files
    off the read-only backup directory. Returns the temp copy's path.
    """
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    tmp_dir = Path(tempfile.mkdtemp(prefix="regwatch-mig-src-"))
    # Remove the private snapshot copy on exit (every path, incl. the raises
    # below) — otherwise each run leaves a full DB copy behind in $TMPDIR.
    atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
    tmp_db = tmp_dir / sqlite_path.name
    shutil.copy2(sqlite_path, tmp_db)

    url = f"sqlite:///{tmp_db.as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()

    if current is None:
        raise MigrationError(
            f"source snapshot {sqlite_path} has no alembic_version stamp — boot the "
            "current app against it once (init_db stamps/upgrades SQLite) and re-snapshot"
        )
    if current != head:
        print(f"source snapshot is at alembic {current}; upgrading temp copy to {head}")
        command.upgrade(cfg, "head")
    return tmp_db


def copy_table(
    src_engine: Engine,
    dst_engine: Engine,
    table: sa.Table,
    *,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Stream one table from source to target in chunked executemany batches.

    Ids are preserved verbatim (explicit-pk inserts). JSON and datetime columns
    round-trip through the shared SQLModel ``Table`` objects, so SQLite TEXT
    JSON lands as Postgres JSONB without call-site conversion.
    """
    copied = 0
    with src_engine.connect() as src_conn, dst_engine.begin() as dst_conn:
        result = src_conn.execution_options(yield_per=batch_size).execute(sa.select(table))
        for partition in result.mappings().partitions(batch_size):
            rows = [dict(r) for r in partition]
            if not rows:
                continue
            dst_conn.execute(table.insert(), rows)
            copied += len(rows)
    return copied


def sequence_reset_statements(tables: Sequence[sa.Table]) -> list[str]:
    """``setval`` statements for every single-column integer-autoincrement PK.

    REQUIRED on Postgres: rows were copied with explicit ids, so the implicit
    SERIAL/IDENTITY sequences still sit at their start value and the next
    INSERT would collide with a copied id. ``setval(seq, max(id)+1, false)``
    makes the next ``nextval()`` return ``max(id)+1`` (or 1 on an empty
    table). ``pg_get_serial_sequence`` resolves both SERIAL and IDENTITY
    sequences, and ``setval`` is strict, so a NULL lookup (no sequence) is a
    harmless no-op. String-pk tables (chat_session, chat_message, chunk) have
    no sequence and are skipped.
    """
    stmts: list[str] = []
    for t in tables:
        pk_cols = list(t.primary_key.columns)
        if len(pk_cols) != 1:
            continue
        col = pk_cols[0]
        if not isinstance(col.type, sa.Integer):
            continue
        stmts.append(
            f"SELECT setval(pg_get_serial_sequence('\"{t.name}\"', '{col.name}'), "
            f'COALESCE((SELECT MAX("{col.name}") FROM "{t.name}"), 0) + 1, false)'
        )
    return stmts


def reset_sequences(dst_engine: Engine, tables: Sequence[sa.Table]) -> int:
    """Apply the setval statements (Postgres only). Returns statements run."""
    if dst_engine.dialect.name != "postgresql":
        return 0
    stmts = sequence_reset_statements(tables)
    with dst_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(sa.text(stmt))
    return len(stmts)


def verify_migration(
    src_engine: Engine,
    dst_engine: Engine,
    tables: Sequence[sa.Table],
    *,
    chunk_source_count: int | None = None,
    chunk_target_count: int | None = None,
) -> list[str]:
    """Per-table source-vs-target counts. Returns the list of mismatch lines.

    This is the load-bearing step: a silent partial copy is the worst failure
    mode, so every table is compared and ANY difference fails the migration.
    """
    failures: list[str] = []
    src_present = existing_tables(src_engine)
    print(f"\n{'table':<32} {'source':>10} {'target':>10}  status")
    for t in tables:
        src_n = count_rows(src_engine, t) if t.name in src_present else 0
        dst_n = count_rows(dst_engine, t)
        ok = src_n == dst_n
        print(f"{t.name:<32} {src_n:>10} {dst_n:>10}  {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(f"{t.name}: source={src_n} target={dst_n}")
    if chunk_source_count is not None:
        label = f"{CHUNK_TABLE} (chroma -> pgvector)"
        dst_n = -1 if chunk_target_count is None else chunk_target_count
        ok = chunk_source_count == dst_n
        print(f"{label:<32} {chunk_source_count:>10} {dst_n:>10}  {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(f"{CHUNK_TABLE}: chroma={chunk_source_count} pgvector={dst_n}")
    return failures


# --------------------------------------------------------------------------
# Runtime wiring (imports the store layer's public surface; see the contract)
# --------------------------------------------------------------------------


def _activate_runtime(database_url: str) -> None:
    """Point the regwatch store layer at the target Postgres.

    The settings singleton is lru-cached and may have been built before the env
    was set, so clear it and reset the cached engine — the same reset hooks the
    test suite uses. The Chroma client reset is deliberately NOT called: it
    would wipe the store it points at, and no client exists yet anyway.
    """
    os.environ["DATABASE_URL"] = database_url
    os.environ["EMBEDDING_PROVIDER"] = "openai"
    import config.settings as cs

    from regwatch.store import db as db_module

    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()


def drop_unstamped_target_tables(engine: Engine, *, truncate: bool) -> bool:
    """Recover a tables-but-no-alembic-stamp target (e.g. a reused rehearsal DB).

    ``init_db()`` refuses that ambiguous state and its error message points
    here ("re-run scripts/migrate_to_supabase.py with --truncate"), so
    --truncate must actually be able to recover it. With --truncate the
    operator has declared the target disposable: DROP the known application
    tables (children first, CASCADE) and let the bootstrap recreate them —
    the only way to guarantee the schema matches a fresh boot. Without
    --truncate, refuse with the same guidance. Returns True if tables were
    dropped.
    """
    present = existing_tables(engine)
    app_tables = (set(SQLModel.metadata.tables) | {CHUNK_TABLE}) & present
    if "alembic_version" in present or not app_tables:
        return False  # stamped (init_db verifies head) or fresh — nothing to do
    if not truncate:
        raise MigrationError(
            "target has regwatch tables but no alembic_version stamp — ambiguous "
            f"state (tables: {', '.join(sorted(app_tables))}); pass --truncate to "
            "drop and rebuild them from scratch"
        )
    drop_order = [CHUNK_TABLE] + [t.name for t in reversed(SQLModel.metadata.sorted_tables)]
    with engine.begin() as conn:
        for name in drop_order:
            if name in present:
                conn.execute(sa.text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
    return True


def bootstrap_target(*, truncate: bool = False) -> Engine:
    """Bootstrap the target schema through the SAME code path the API uses.

    ``init_db()`` on a fresh Postgres runs ``SQLModel.metadata.create_all`` +
    pgvector DDL + RLS + ``alembic stamp head`` (no SQLite migration-history
    replay); on a stamped database it verifies the head matches. The dialect
    guard runs FIRST so a store layer that does not honor DATABASE_URL can
    never make this script touch the live SQLite/Chroma paths.
    """
    from regwatch.store import db as db_module

    engine = db_module.get_engine()
    if engine.dialect.name != "postgresql":
        raise MigrationError(
            "the store layer did not pick up DATABASE_URL "
            f"(engine dialect is {engine.dialect.name!r}); refusing to run — "
            "this build predates the Postgres-capable store layer"
        )
    if drop_unstamped_target_tables(engine, truncate=truncate):
        print("dropped unstamped regwatch tables from target (--truncate recovery)")
    try:
        db_module.init_db()
    except RuntimeError as exc:
        # e.g. stamped at a different alembic revision than this build's head —
        # surface it as a clean configuration error, not a traceback.
        raise MigrationError(str(exc)) from exc
    # A fresh-bootstrap target is `create_all` + `stamp head`, which NEVER replays
    # migration 0011 - so the `ensure_rls` event trigger that auto-RLSes
    # future tables would be missing on a freshly-migrated DB. Apply its same
    # idempotent DDL here so a migrated database matches an upgrade-replayed one.
    ensure_rls_event_trigger(engine)
    return engine


def ensure_rls_event_trigger(engine: Engine) -> None:
    """(Re)create the `ensure_rls` event trigger on the Postgres target.

    Loads the canonical idempotent DDL from migration 0011 (the single source of
    truth for the trigger), so the fresh-bootstrap path and the
    ``alembic upgrade`` path converge on identical objects. No-op off Postgres.
    """
    if engine.dialect.name != "postgresql":
        return
    path = ROOT / "migrations" / "versions" / "0011_ensure_rls_event_trigger.py"
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise MigrationError(f"cannot load migration 0011 from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with engine.begin() as conn:
        for stmt in mod.rls_event_trigger_sql():
            conn.execute(sa.text(stmt))


def stamp_alembic_head(dst_engine: Engine) -> str:
    """Idempotently stamp the target at the migrations head (step 5).

    Uses MigrationContext directly (not env.py) so the stamp is independent of
    the alembic environment's URL resolution. The fresh-bootstrap path already
    stamped; this re-asserts it and covers a --truncate re-run.
    """
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(ROOT / "migrations"))
    head = script.get_current_head()
    if head is None:
        raise MigrationError("no alembic head found under migrations/")
    with dst_engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        if ctx.get_current_revision() != head:
            ctx.stamp(script, head)
    return head


# --------------------------------------------------------------------------
# Chroma -> pgvector chunk migration
# --------------------------------------------------------------------------


def open_chroma_collection(chroma_dir: Path) -> Any:
    """Open the snapshot Chroma collection read-only (direct client, NOT the
    vector_store dispatcher — that one points at pgvector in DATABASE_URL mode)."""
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    from regwatch.store.vector_store import COLLECTION

    client = chromadb.PersistentClient(
        path=str(chroma_dir),
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    try:
        return client.get_collection(COLLECTION)
    except Exception as exc:
        raise MigrationError(
            f"chroma collection {COLLECTION!r} not found under {chroma_dir} — "
            "is --chroma pointing at a snapshot of data/chroma?"
        ) from exc


def iter_chroma_chunks(
    collection: Any, *, batch_size: int = EMBED_BATCH_SIZE
) -> Iterator[tuple[list[str], list[str], list[dict[str, Any]]]]:
    """Yield (ids, documents, metadatas) pages from the snapshot collection."""
    total = int(collection.count())
    offset = 0
    while offset < total:
        res = collection.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        ids = list(res.get("ids") or [])
        if not ids:
            break
        docs = [d or "" for d in (res.get("documents") or [])]
        metas = [dict(m or {}) for m in (res.get("metadatas") or [])]
        yield ids, docs, metas
        offset += len(ids)


def migrate_chunks(chroma_dir: Path) -> tuple[int, int]:
    """Re-embed every Chroma chunk via OpenAI and insert through the vector
    store's public surface (which dispatches to pgvector in DATABASE_URL mode).

    Returns ``(chroma_count, pgvector_count)`` for verification.
    """
    if importlib.util.find_spec("regwatch.store.pgvector_store") is None:
        raise MigrationError(
            "regwatch.store.pgvector_store is not available — the store layer "
            "predates the pgvector backend; refusing to write chunks anywhere else"
        )
    from regwatch.process.embedder import get_embedding_provider
    from regwatch.store import vector_store

    provider = get_embedding_provider("openai")
    if int(provider.dim) != CHUNK_EMBEDDING_DIM:
        raise MigrationError(
            f"embedding provider dim {provider.dim} != chunk table dim {CHUNK_EMBEDDING_DIM}"
        )

    collection = open_chroma_collection(chroma_dir)
    total = int(collection.count())
    done = 0
    for ids, docs, metas in iter_chroma_chunks(collection):
        embeddings = provider.embed(docs)
        vector_store.add_chunks(ids=ids, embeddings=embeddings, documents=docs, metadatas=metas)
        done += len(ids)
        print(f"  chunks: {done}/{total} re-embedded + inserted")
    return total, int(vector_store.collection_size())


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate a SQLite + Chroma snapshot to Postgres (Supabase) + pgvector.",
    )
    parser.add_argument("--sqlite", type=Path, required=True, help="path to a regwatch.db SNAPSHOT")
    parser.add_argument("--chroma", type=Path, required=True, help="path to a chroma dir SNAPSHOT")
    parser.add_argument(
        "--database-url", required=True, help="target Postgres URL (Supabase session pooler)"
    )
    parser.add_argument(
        "--skip-embed",
        action="store_true",
        help="skip the Chroma -> pgvector chunk step (relational copy rehearsal)",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="wipe the target application tables before copying (idempotent re-run)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        database_url = normalize_database_url(args.database_url)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sqlite_path: Path = args.sqlite
    if not sqlite_path.is_file():
        print(f"error: --sqlite {sqlite_path} is not a file", file=sys.stderr)
        return 2
    if not args.skip_embed and not args.chroma.is_dir():
        print(f"error: --chroma {args.chroma} is not a directory", file=sys.stderr)
        return 2

    _activate_runtime(database_url)

    # (1) bootstrap the target schema (same code path as the API).
    try:
        dst_engine = bootstrap_target(truncate=args.truncate)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("target schema bootstrapped (create_all + RLS + alembic stamp)")

    if not args.skip_embed:
        from config.settings import get_settings

        if not get_settings().openai_api_key:
            print(
                "error: OPENAI_API_KEY is required to re-embed chunks "
                "(or pass --skip-embed to rehearse the relational copy)",
                file=sys.stderr,
            )
            return 2

    # Source is read through a private temp copy, alembic-upgraded to head if
    # the snapshot was stamped behind it (the snapshot itself is never written).
    try:
        src_db = upgrade_source_snapshot(sqlite_path)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    src_engine = sa.create_engine(f"sqlite:///{src_db.as_posix()}")
    # Dispose the source engine on exit (runs before the temp-dir rmtree, LIFO).
    atexit.register(src_engine.dispose)
    tables = ordered_tables()

    try:
        prepare_target(dst_engine, tables, truncate=args.truncate)
    except MigrationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3

    # (2) copy every SQLModel table in FK-dependency order, ids preserved.
    src_present = existing_tables(src_engine)
    for table in tables:
        if table.name not in src_present:
            print(f"  - {table.name}: not in source snapshot, skipped (target left empty)")
            continue
        n = copy_table(src_engine, dst_engine, table, batch_size=BATCH_SIZE)
        print(f"  - {table.name}: copied {n} rows")

    n_seq = reset_sequences(dst_engine, tables)
    print(f"reset {n_seq} Postgres sequences via setval (id-collision guard)")

    # (3) Chroma -> pgvector, re-embedded through the OpenAI provider.
    chunk_source_count: int | None = None
    chunk_target_count: int | None = None
    if args.skip_embed:
        print("--skip-embed: chunk migration NOT performed; pgvector `chunk` is empty")
    else:
        try:
            chunk_source_count, chunk_target_count = migrate_chunks(args.chroma)
        except MigrationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # (4) verification — exit nonzero on ANY mismatch.
    failures = verify_migration(
        src_engine,
        dst_engine,
        tables,
        chunk_source_count=chunk_source_count,
        chunk_target_count=chunk_target_count,
    )
    if failures:
        print("\nVERIFICATION FAILED:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    # (5) stamp alembic head (idempotent re-assert).
    head = stamp_alembic_head(dst_engine)
    print(f"\nalembic stamped at {head}")
    print("migration complete — all counts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
