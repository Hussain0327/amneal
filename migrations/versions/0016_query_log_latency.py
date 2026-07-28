"""additive nullable query_log.latency_ms for the provider-cutover p95 gate

The Databricks rollout (docs/DATABRICKS_ADOPTION_2026-07-28.md, steps 2 and 7)
gates each provider flip on a latency comparison against the pre-flip week.
There was no server-side latency column to compare against: ``query_log``
recorded what was answered, never how long it took.

``latency_ms`` is the wall time from turn start to the audit write, stamped by
whichever control plane owns the turn -- Go's ``persistTurn`` on the native
path, Python's ``ask()`` on the relay/stream path. It is NULLABLE and stays
NULL for turns written before this migration, for the -1 audit-store-down
sentinel path, and for any writer that does not measure -- never 0, which
would read as an impossibly fast turn in a percentile.

Additive only: one nullable column with no default, which Postgres 11+ applies
as a catalog-only change (no table rewrite, no per-row work). ``query_log`` is
the hottest write path in the app, so the DDL still runs under a bounded
``lock_timeout``: a blocked ADD COLUMN behind a long-running reader would
otherwise queue every INSERT behind it (the 2026-06-18 chunk-table lock
pileup). Failing fast and retrying is correct; blocking the audit path is not.

Revision ID: 0016_query_log_latency
Revises: 0015_embedding_profiles
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_query_log_latency"
down_revision: str | None = "0015_embedding_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bounded so a blocked ADD COLUMN fails the release command instead of pinning
# every concurrent audit INSERT behind an unacquirable ACCESS EXCLUSIVE lock.
_LOCK_TIMEOUT = "10s"


def _bounded_lock(bind: sa.engine.Connection) -> None:
    """Apply the lock timeout on Postgres; a no-op on other backends."""
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")


def upgrade() -> None:
    _bounded_lock(op.get_bind())
    op.add_column("query_log", sa.Column("latency_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    _bounded_lock(op.get_bind())
    op.drop_column("query_log", "latency_ms")
