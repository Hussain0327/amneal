"""durable watch_run ledger

Persists one row per COMPLETED Watch run so the UI can distinguish a quiet day
(recent run, zero alerts) from a cron that has been dead for a week -- the only
prior record was a JSONL digest on the GitHub runner's ephemeral disk. Pure
``create_table`` + ``create_index``: dialect-portable, so it applies to the
live Supabase Postgres via ``alembic upgrade head`` AND to SQLite via
``_init_sqlite``'s ``command.upgrade``. A FRESH Postgres never replays this
file -- its bootstrap is ``create_all`` + ``stamp head``, and ``models.py``'s
``WatchRun`` declares the same columns/index, so both paths produce the same
schema.

No RLS DDL needed here (verified against 0011): migration 0011's ``ensure_rls``
event trigger fires on every plain ``CREATE TABLE`` in ``public`` and enables
deny-all RLS on the new table inside the same transaction, and the fresh-boot
paths additionally run the ``store/db.py`` RLS sweep over all public tables --
so ``watch_run`` is covered on every bootstrap route without extra statements.

Revision ID: 0012_watch_runs
Revises: 0011_ensure_rls_event_trigger
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_watch_runs"
down_revision: str | None = "0011_ensure_rls_event_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watch_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("listings", sa.Integer(), nullable=False),
        sa.Column("matched", sa.Integer(), nullable=False),
        sa.Column("added", sa.Integer(), nullable=False),
        sa.Column("revised", sa.Integer(), nullable=False),
        sa.Column("unchanged", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("alerts", sa.Integer(), nullable=False),
        sa.Column("digest_date", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watch_run_finished_at", "watch_run", ["finished_at"])


def downgrade() -> None:
    op.drop_index("ix_watch_run_finished_at", table_name="watch_run")
    op.drop_table("watch_run")
