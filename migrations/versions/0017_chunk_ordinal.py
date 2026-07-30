"""additive nullable chunk.ordinal for deterministic in-document ordering

The chunker has always computed an ordinal per chunk (it is the third field
of the id string ``{doc_id}-{version_id}-{ordinal}``) but ``add_chunks``
dropped it, so sibling/adjacency ordering could only be recovered by parsing
id strings. The chunk-cleanup wave (chunking recipe v2) and the knowledge-
graph layer both need it as a real column: section membership rank and
"primary chunk" selection are ordinal-based.

Additive only: one nullable INTEGER with no default (catalog-only on
Postgres 11+). The backfill UPDATE parses the ordinal out of well-formed id
strings; rows with unexpected ids stay NULL rather than failing the release.
``chunk`` is the table behind the 2026-06-18 lock pileup, so the DDL runs
under a bounded ``lock_timeout`` -- fail fast and retry, never queue live
readers behind an unacquirable ACCESS EXCLUSIVE lock.

Revision ID: 0017_chunk_ordinal
Revises: 0016_query_log_latency
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_chunk_ordinal"
down_revision: str | None = "0016_query_log_latency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "10s"


def _bounded_lock(bind: sa.engine.Connection) -> None:
    """Apply the lock timeout on Postgres; a no-op on other backends."""
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # `chunk` is raw-DDL and Postgres-only (db.py bootstrap); it does not
        # exist on other backends' migration replays (same guard as 0015).
        return
    _bounded_lock(bind)
    op.add_column("chunk", sa.Column("ordinal", sa.Integer(), nullable=True))
    # Plain row UPDATE (no ACCESS EXCLUSIVE lock); ~10k rows in prod.
    bind.exec_driver_sql(
        "UPDATE chunk SET ordinal = split_part(id, '-', 3)::integer "
        "WHERE ordinal IS NULL AND id ~ '^[0-9]+-[0-9]+-[0-9]+$'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)
    op.drop_column("chunk", "ordinal")
