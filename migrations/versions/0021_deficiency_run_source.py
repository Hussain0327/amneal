"""record which surface submitted a deficiency_run

The Compliance Studio check runs the same detection pipeline as the PDF upload
path -- same state machine, same limiter, same audit gating -- over a document
assembled from editor blocks instead of a parsed PDF. Giving it its own table
would duplicate the compare-and-set transitions and the INV-6 audit ordering
for no behaviour that differs.

What DOES differ is who may read the result. An uploaded submission is a
deliberate, finished artifact and its runs are org-shared, matching
``whitepaper_run``. A Studio check is run over an analyst's unfinished draft
and its fault report quotes that draft verbatim, so it is private to the
analyst who ran it. One nullable column carries that distinction: NULL is the
pre-existing upload path, "studio" is a check.

Additive and nullable on purpose. Every existing row is an upload, and NULL
already means exactly that, so no backfill runs and no read path changes
meaning for data written before this migration. ALTER TABLE ... ADD COLUMN with
no default and no NOT NULL is metadata-only on PostgreSQL 11+: it does not
rewrite the table, so it cannot lock out live readers.

The index is on the filter every read path now applies (uploads_only in
store/deficiency_runs.list_runs, and the per-surface guards in api/main). It is
created without CONCURRENTLY because the table is small and this migration
already holds a brief ACCESS EXCLUSIVE lock for the ADD COLUMN.

Revision ID: 0021_deficiency_run_source
Revises: 0020_eval_run
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_deficiency_run_source"
down_revision: str | None = "0020_eval_run"
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
        return
    _bounded_lock(bind)
    op.add_column("deficiency_run", sa.Column("source", sa.String(), nullable=True))
    op.create_index("ix_deficiency_run_source", "deficiency_run", ["source"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)
    # Reversible without data loss for the upload path: every row this column
    # distinguishes as a Studio check becomes indistinguishable from an upload
    # again, which is what the schema meant before this revision. Downgrading
    # with Studio rows present would make them org-visible, so the caller is
    # expected to delete them first -- this migration will not do it silently.
    op.drop_index("ix_deficiency_run_source", table_name="deficiency_run")
    op.drop_column("deficiency_run", "source")
