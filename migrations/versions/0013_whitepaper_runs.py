"""durable white-paper runs + attributed analyst inputs

Persists one row per White-Paper populate (Phase 2) so a run survives a page
refresh and becomes an org-shared document: ``whitepaper_run`` holds the
IMMUTABLE generated payload (fingerprinted via ``sections_sha256``, INV-3) and
``whitepaper_input`` holds the attributed analyst overlay -- one CURRENT value
per (run, cell), structurally separate so a human answer never mutates the
cited generated layer. Pure ``create_table`` + ``create_index``:
dialect-portable, so it applies to the live Supabase Postgres via ``alembic
upgrade head`` AND to SQLite via ``_init_sqlite``'s ``command.upgrade``. A
FRESH Postgres never replays this file -- its bootstrap is ``create_all`` +
``stamp head``, and ``models.py``'s ``WhitepaperRun``/``WhitepaperInput``
declare the same columns/constraints/indexes, so both paths must produce the
same schema (keep them in lockstep).

No RLS DDL needed here (same as 0012): migration 0011's ``ensure_rls`` event
trigger fires on every plain ``CREATE TABLE`` in ``public`` and enables
deny-all RLS on the new tables inside the same transaction, and the fresh-boot
paths additionally run the ``store/db.py`` RLS sweep over all public tables --
so both tables are covered on every bootstrap route without extra statements.

Revision ID: 0013_whitepaper_runs
Revises: 0012_watch_runs
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_whitepaper_runs"
down_revision: str | None = "0012_watch_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whitepaper_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("rld_name_input", sa.String(), nullable=False),
        sa.Column("application_number", sa.String(), nullable=False),
        sa.Column("application_type", sa.String(), nullable=False),
        sa.Column("ingredient", sa.String(), nullable=False),
        sa.Column("normalized_name", sa.String(), nullable=False),
        # _json_column() emits nullable JSON (sa_column overrides SQLModel's
        # non-null inference) -- every JSON column in this schema is nullable
        # at the DDL level; keep the migration converged with create_all.
        sa.Column("spine_json", sa.JSON(), nullable=True),
        sa.Column("sections_json", sa.JSON(), nullable=True),
        sa.Column("warnings_json", sa.JSON(), nullable=True),
        sa.Column("sections_sha256", sa.String(), nullable=False),
        sa.Column("source_audit_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.Column("finalized_by_user_id", sa.Integer(), nullable=True),
        sa.Column("populated_count", sa.Integer(), nullable=False),
        sa.Column("analyst_input_count", sa.Integer(), nullable=False),
        sa.Column("verified_absent_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["finalized_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('draft', 'final')", name="ck_whitepaper_run_status"),
    )
    op.create_index("ix_whitepaper_run_created_at", "whitepaper_run", ["created_at"])
    op.create_index(
        "ix_whitepaper_run_created_by_user_id", "whitepaper_run", ["created_by_user_id"]
    )
    op.create_index(
        "ix_whitepaper_run_application_number", "whitepaper_run", ["application_number"]
    )
    op.create_index("ix_whitepaper_run_normalized_name", "whitepaper_run", ["normalized_name"])
    op.create_index("ix_whitepaper_run_source_audit_id", "whitepaper_run", ["source_audit_id"])
    op.create_index("ix_whitepaper_run_status", "whitepaper_run", ["status"])

    op.create_table(
        "whitepaper_input",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("cell_id", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("author_user_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["whitepaper_run.id"]),
        sa.ForeignKeyConstraint(["author_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "cell_id", name="uq_whitepaper_input_run_cell"),
    )
    op.create_index("ix_whitepaper_input_run_id", "whitepaper_input", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_whitepaper_input_run_id", table_name="whitepaper_input")
    op.drop_table("whitepaper_input")

    op.drop_index("ix_whitepaper_run_status", table_name="whitepaper_run")
    op.drop_index("ix_whitepaper_run_source_audit_id", table_name="whitepaper_run")
    op.drop_index("ix_whitepaper_run_normalized_name", table_name="whitepaper_run")
    op.drop_index("ix_whitepaper_run_application_number", table_name="whitepaper_run")
    op.drop_index("ix_whitepaper_run_created_by_user_id", table_name="whitepaper_run")
    op.drop_index("ix_whitepaper_run_created_at", table_name="whitepaper_run")
    op.drop_table("whitepaper_run")
