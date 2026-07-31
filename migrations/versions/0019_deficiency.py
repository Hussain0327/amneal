"""deficiency analysis: deficiency_run job ledger + deficiency_kb precedent store

Two additive tables for the DefPredict integration (DECISIONS.md 2026-07-30):

* ``deficiency_run`` -- one row per uploaded-PDF analysis job, created BEFORE
  the work runs (pending/running/complete/failed; the UI polls it). Mirrors
  the SQLModel class in store/models.py.
* ``deficiency_kb`` -- the historical-deficiency precedent corpus searched by
  the detection pipeline; vector(1024) embeddings from the Databricks Qwen3
  endpoint (in-tenant, D1). Mirrors the Core table in store/deficiency_kb.py.
  Deliberately OUTSIDE the chunk embedding-profile machinery so its rows never
  join a profile backfill denominator while the embedding flip is in flight.
  Expected scale ~500 rows: exact scan, no vector index.

Both tables are new and empty: FK creation takes only brief metadata locks,
nothing references them, downgrade drops them cleanly. Neither enters the Go
sqlc schema snapshot (gen-store-schema.sh scopes to its 7 tables). The 0011
event trigger auto-applies deny-all RLS at CREATE time.

Postgres-gated like 0018: fresh Postgres bootstraps these via create_all (the
tables are registered in SQLModel.metadata by store/models.py and
store/deficiency_kb.py, both imported by the bootstrap path in store/db.py)
and never replays this file; non-Postgres replays skip it because ``vector``
is a Postgres-only type. The pgvector extension schema varies (Supabase
installs it in ``extensions``), so the search_path is widened
transaction-locally before emitting the unqualified VECTOR(1024) DDL --
same technique as migration 0015.

Revision ID: 0019_deficiency
Revises: 0018_knowledge_graph
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0019_deficiency"
down_revision: str | None = "0018_knowledge_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "10s"


def _widen_search_path_for_vector(bind: sa.engine.Connection) -> None:
    """Make the unqualified VECTOR type resolvable for this transaction.

    0015 guarantees the extension exists by the time this revision runs in an
    upgrade chain; a missing extension here means this file is being replayed
    out of order, which deserves a loud stop, not a fallback CREATE.
    """
    vector_schema = bind.execute(
        sa.text(
            "SELECT n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = 'vector'"
        )
    ).scalar()
    if vector_schema is None:
        raise RuntimeError(
            "pgvector extension is not installed; migration 0015 must run before 0019"
        )
    if vector_schema not in {"public", "extensions"}:
        raise RuntimeError(
            "pgvector must be installed in the public or extensions schema; "
            f"found {vector_schema!r}"
        )
    bind.execute(
        sa.text("SELECT set_config('search_path', :path, true)"),
        {"path": f'public,"{vector_schema}"'},
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")

    op.create_table(
        "deficiency_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("fault_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        # JSONB directly (not sa.JSON): this migration is Postgres-gated, and
        # create_all's _json_column() variant emits JSONB -- keep both paths
        # producing the same type.
        sa.Column("report_json", JSONB(), nullable=True),
        sa.Column("audit_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'failed')",
            name="ck_deficiency_run_status",
        ),
    )
    op.create_index("ix_deficiency_run_created_at", "deficiency_run", ["created_at"])
    op.create_index(
        "ix_deficiency_run_created_by_user_id", "deficiency_run", ["created_by_user_id"]
    )
    op.create_index("ix_deficiency_run_status", "deficiency_run", ["status"])
    op.create_index("ix_deficiency_run_audit_id", "deficiency_run", ["audit_id"])

    _widen_search_path_for_vector(bind)
    op.create_table(
        "deficiency_kb",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("anda_number", sa.String(), nullable=False, server_default=""),
        sa.Column("product_name", sa.String(), nullable=False, server_default=""),
        sa.Column("deficiency_text", sa.Text(), nullable=False),
        sa.Column("deficiency_type", sa.String(), nullable=True),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(deficiency_text) > 0",
            name="ck_deficiency_kb_text_nonempty",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_table("deficiency_kb")
    op.drop_index("ix_deficiency_run_audit_id", table_name="deficiency_run")
    op.drop_index("ix_deficiency_run_status", table_name="deficiency_run")
    op.drop_index("ix_deficiency_run_created_by_user_id", table_name="deficiency_run")
    op.drop_index("ix_deficiency_run_created_at", table_name="deficiency_run")
    op.drop_table("deficiency_run")
