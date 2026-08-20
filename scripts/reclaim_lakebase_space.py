"""Operator tool: report Lakebase branch storage and (optionally) reclaim it.

Lakebase's branch is capped at 512 MiB (``branch_logical_size_limit_bytes``,
tier-fixed and not raisable via the API). Measured headroom has been as low as
~21 MiB. The legacy ``ix_chunk_embedding_hnsw`` index (on ``chunk.embedding``)
and the legacy embeddings themselves are dead weight once a named embedding
profile serves retrieval (see ``retrieve/retriever.py:220-276`` -- the legacy
column is read only when ``active_embedding_profile == "legacy"``).

Usage:
    # Read-only report (default -- makes no writes):
    .venv/bin/python scripts/reclaim_lakebase_space.py

    # Actually reclaim space (drop the legacy index, null out legacy
    # embeddings in small batches, vacuum):
    .venv/bin/python scripts/reclaim_lakebase_space.py --apply

Every DB call carries an explicit ``statement_timeout`` (set on the session,
independent of the app's ambient DB_STATEMENT_TIMEOUT config) so a stalled
query cannot hold a lock or a connection indefinitely against an already
storage-constrained branch.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy import text as sa_text

# Lakebase branch cap, MiB. Tier-fixed; not discoverable via a Postgres system
# view, so it is a constant here rather than a query result. See module
# docstring / the 2026-08-18 free-tier incident.
_DEFAULT_BRANCH_LIMIT_MB = 512

# `pg_database_size` under-counts the Lakebase branch's own accounting by a
# measured ~23 MiB (515,080,192 logical_size_bytes vs. 491,446,272
# pg_database_size on the same snapshot) -- Postgres has no view onto whatever
# Lakebase charges beyond the database itself. Treat pg_database_size as a
# conservative proxy, not an exact match to the branch API's number.
_DEFAULT_MIN_FREE_MB = 10
_DEFAULT_BATCH_SIZE = 500
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000

# Both names are live in the wild: the index ships as ix_chunk_embedding_hnsw
# (a misnomer -- it is ON public.chunk, not chunk_embedding) and the rename
# migration may or may not have run yet on a given branch. Handle either,
# or the reclaim silently skips ~42 MiB and reports "not present".
_LEGACY_HNSW_INDEXES = (
    "ix_chunk_legacy_embedding_hnsw",
    "ix_chunk_embedding_hnsw",
)
_LEGACY_HNSW_INDEX = _LEGACY_HNSW_INDEXES[0]
_REPORT_TABLES = ("chunk", "chunk_embedding")


@dataclass(frozen=True)
class TableSizes:
    """Heap/TOAST/index byte breakdown for one table."""

    table: str
    heap_bytes: int
    toast_bytes: int
    index_bytes: int
    total_bytes: int


def _set_statement_timeout(conn, timeout_ms: int) -> None:
    """Session-level statement_timeout, explicit and independent of app config.

    Uses ``SET`` (session-scoped) rather than ``SET LOCAL`` so it also covers
    autocommit connections (VACUUM cannot run inside a transaction block).
    """
    conn.execute(sa_text(f"SET statement_timeout = {int(timeout_ms)}"))


def _pg_database_size(engine: Engine, timeout_ms: int) -> int:
    with engine.connect() as conn:
        _set_statement_timeout(conn, timeout_ms)
        return int(
            conn.execute(sa_text("SELECT pg_database_size(current_database())")).scalar() or 0
        )


def _table_sizes(engine: Engine, table: str, timeout_ms: int) -> TableSizes | None:
    """Heap/TOAST/index breakdown for one table, or None if it doesn't exist."""
    with engine.connect() as conn:
        _set_statement_timeout(conn, timeout_ms)
        row = (
            conn.execute(
                sa_text(
                    "SELECT pg_relation_size(c.oid) AS heap_bytes, "
                    "COALESCE(pg_relation_size(c.reltoastrelid), 0) AS toast_bytes, "
                    "pg_indexes_size(c.oid) AS index_bytes, "
                    "pg_total_relation_size(c.oid) AS total_bytes "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = :table AND c.relkind = 'r'"
                ),
                {"table": table},
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    return TableSizes(
        table=table,
        heap_bytes=int(row["heap_bytes"]),
        toast_bytes=int(row["toast_bytes"]),
        index_bytes=int(row["index_bytes"]),
        total_bytes=int(row["total_bytes"]),
    )


def _index_size_bytes(engine: Engine, index_name: str, timeout_ms: int) -> int | None:
    """Bytes for one index, or None if it does not exist (e.g. already dropped)."""
    with engine.connect() as conn:
        _set_statement_timeout(conn, timeout_ms)
        row = conn.execute(
            sa_text(
                "SELECT pg_relation_size(c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = :name AND c.relkind = 'i'"
            ),
            {"name": index_name},
        ).scalar()
    return int(row) if row is not None else None


def _mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MiB"


def print_report(engine: Engine, timeout_ms: int, *, label: str) -> None:
    """Prints pg_database_size, per-table heap/toast/index, and the legacy index."""
    db_size = _pg_database_size(engine, timeout_ms)
    print(f"--- {label} ---")
    print(f"pg_database_size: {db_size} bytes ({_mb(db_size)})")
    for table in _REPORT_TABLES:
        sizes = _table_sizes(engine, table, timeout_ms)
        if sizes is None:
            print(f"  {table}: (table not found)")
            continue
        print(
            f"  {table}: heap={_mb(sizes.heap_bytes)} toast={_mb(sizes.toast_bytes)} "
            f"index={_mb(sizes.index_bytes)} total={_mb(sizes.total_bytes)}"
        )
    hnsw_bytes = (
        sum(_index_size_bytes(engine, name, timeout_ms) or 0 for name in _LEGACY_HNSW_INDEXES)
        or None
    )
    if hnsw_bytes is None:
        print("  legacy chunk.embedding HNSW: (not present)")
    else:
        print(f"  legacy chunk.embedding HNSW: {_mb(hnsw_bytes)}")


def _drop_legacy_hnsw_index(engine: Engine, timeout_ms: int, lock_timeout_ms: int) -> None:
    with engine.begin() as conn:
        _set_statement_timeout(conn, timeout_ms)
        conn.execute(sa_text(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}"))
        for name in _LEGACY_HNSW_INDEXES:
            conn.execute(sa_text(f"DROP INDEX IF EXISTS {name}"))
    print(f"dropped index(es) {', '.join(_LEGACY_HNSW_INDEXES)} (if they existed)")


def _null_out_legacy_embeddings(
    engine: Engine,
    *,
    batch_size: int,
    min_free_mb: int,
    branch_limit_bytes: int,
    timeout_ms: int,
) -> int:
    """Batched, committed-per-batch UPDATE that clears chunk.embedding.

    A single unbatched UPDATE across all legacy rows would create thousands of
    dead tuples at once against a branch with single-digit MiB of headroom.
    Batching bounds that, and the free-space check between batches aborts
    before a batch that would push the branch over its cap.
    """
    total_updated = 0
    while True:
        free_bytes = branch_limit_bytes - _pg_database_size(engine, timeout_ms)
        min_free_bytes = min_free_mb * 1024 * 1024
        if free_bytes < min_free_bytes:
            raise RuntimeError(
                f"aborting: free space {_mb(free_bytes)} is below the "
                f"--min-free-mb floor ({min_free_mb} MiB); {total_updated} rows "
                "already nulled and committed in prior batches"
            )
        with engine.begin() as conn:
            _set_statement_timeout(conn, timeout_ms)
            result = conn.execute(
                sa_text(
                    "UPDATE chunk SET embedding = NULL "
                    "WHERE id IN (SELECT id FROM chunk WHERE embedding IS NOT NULL "
                    "LIMIT :batch_size)"
                ),
                {"batch_size": batch_size},
            )
            updated = int(result.rowcount or 0)
        total_updated += updated
        print(f"  batch: nulled {updated} rows (running total {total_updated})")
        if updated == 0:
            break
    return total_updated


def _vacuum_chunk(engine: Engine, timeout_ms: int) -> None:
    # VACUUM cannot run inside a transaction block; use an autocommit
    # connection with the same explicit statement_timeout guarantee.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        _set_statement_timeout(conn, timeout_ms)
        conn.execute(sa_text("VACUUM (ANALYZE) chunk"))
    print("VACUUM (ANALYZE) chunk complete")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Report Lakebase branch storage for the chunk/chunk_embedding "
            "tables and, with --apply, reclaim space held by the legacy "
            "chunk.embedding column and its HNSW index."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Perform the reclaim (drop legacy index, null legacy embeddings, "
        "vacuum). Without this flag the script only prints the report.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"Rows per UPDATE batch (default {_DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--min-free-mb",
        type=int,
        default=_DEFAULT_MIN_FREE_MB,
        help=f"Abort a batch if free branch space drops below this many MiB "
        f"(default {_DEFAULT_MIN_FREE_MB}).",
    )
    parser.add_argument(
        "--branch-limit-mb",
        type=int,
        default=_DEFAULT_BRANCH_LIMIT_MB,
        help=f"Lakebase branch_logical_size_limit_bytes in MiB (default "
        f"{_DEFAULT_BRANCH_LIMIT_MB}; tier-fixed, override only if the tier "
        "changes).",
    )
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=_DEFAULT_STATEMENT_TIMEOUT_MS,
        help=f"Per-statement timeout in ms for every DB call, including "
        f"VACUUM (default {_DEFAULT_STATEMENT_TIMEOUT_MS}).",
    )
    parser.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=3_000,
        help="lock_timeout in ms for the DROP INDEX statement (default 3000).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Deferred imports: keep --help usable without a configured DATABASE_URL or
    # a full app boot.
    from regwatch.store.db import get_engine

    engine = get_engine()
    branch_limit_bytes = args.branch_limit_mb * 1024 * 1024

    print_report(engine, args.statement_timeout_ms, label="before")

    if not args.apply:
        print("\n--apply not set: read-only report only, no changes made.")
        return 0

    print("\napplying reclaim plan:")
    print(f"  1. DROP INDEX IF EXISTS {' / '.join(_LEGACY_HNSW_INDEXES)}")
    print(f"  2. null out chunk.embedding, batched at {args.batch_size} rows/batch")
    print("  3. VACUUM (ANALYZE) chunk")

    _drop_legacy_hnsw_index(engine, args.statement_timeout_ms, args.lock_timeout_ms)
    try:
        _null_out_legacy_embeddings(
            engine,
            batch_size=args.batch_size,
            min_free_mb=args.min_free_mb,
            branch_limit_bytes=branch_limit_bytes,
            timeout_ms=args.statement_timeout_ms,
        )
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print_report(engine, args.statement_timeout_ms, label="after (aborted early)")
        return 1
    _vacuum_chunk(engine, args.statement_timeout_ms)

    print_report(engine, args.statement_timeout_ms, label="after")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
