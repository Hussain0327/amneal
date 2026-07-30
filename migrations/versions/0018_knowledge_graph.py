"""tier-1 knowledge-graph tables: graph_node / graph_edge / graph_node_chunk

Additive foundation for graph-assisted retrieval (see DECISIONS.md, Jul 30
2026): nodes/edges derived deterministically from the PSG spine at chunk-write
time, refs pointing AT chunks (the only citable unit, INV-1). No runtime path
reads these tables yet -- traversal ships behind a flag in a later PR -- so
this migration is pure structure with zero behavior change.

All three tables are new and empty: FK creation takes only brief metadata
locks (nothing to validate), nothing outside the graph references them, and
downgrade drops them cleanly. None of them enter the Go sqlc schema snapshot
(gen-store-schema.sh scopes to its 7 tables), so the drift lane is untouched.
The 0011 event trigger auto-applies deny-all RLS at CREATE time.

Postgres-gated like 0015/0017: fresh Postgres bootstraps these via create_all
(the tables are registered in SQLModel.metadata by store/graph_store.py) and
never replays this file; non-Postgres replays (the schema-parity test's SQLite
pass) skip it because `chunk`, the FK target, is raw-DDL Postgres-only.

Revision ID: 0018_knowledge_graph
Revises: 0017_chunk_ordinal
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018_knowledge_graph"
down_revision: str | None = "0017_chunk_ordinal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "10s"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.create_table(
        "graph_node",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("node_type", sa.String(), nullable=False),
        sa.Column("natural_key", sa.String(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("attrs_json", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "node_type IN ('application','psg_doc','psg_section')",
            name="ck_graph_node_type",
        ),
        sa.UniqueConstraint("node_type", "natural_key", name="uq_graph_node_type_key"),
    )
    op.create_table(
        "graph_edge",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "src_node_id",
            sa.String(),
            sa.ForeignKey("graph_node.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dst_node_id",
            sa.String(),
            sa.ForeignKey("graph_node.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("edge_type", sa.String(), nullable=False),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("provenance_json", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("tier IN (1, 2)", name="ck_graph_edge_tier"),
        sa.CheckConstraint("confidence > 0 AND confidence <= 1", name="ck_graph_edge_confidence"),
        sa.UniqueConstraint("src_node_id", "dst_node_id", "edge_type", name="uq_graph_edge"),
    )
    op.create_index("ix_graph_edge_src", "graph_edge", ["src_node_id", "edge_type"])
    op.create_index("ix_graph_edge_dst", "graph_edge", ["dst_node_id", "edge_type"])
    op.create_table(
        "graph_node_chunk",
        sa.Column(
            "node_id",
            sa.String(),
            sa.ForeignKey("graph_node.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "chunk_id",
            sa.String(),
            sa.ForeignKey("chunk.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ref_type", sa.String(), primary_key=True),
        sa.Column("rank", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("method", sa.String(), nullable=False),
        sa.CheckConstraint(
            "ref_type IN ('primary','member','mention')",
            name="ck_graph_node_chunk_ref_type",
        ),
    )
    op.create_index("ix_graph_node_chunk_chunk", "graph_node_chunk", ["chunk_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_table("graph_node_chunk")
    op.drop_table("graph_edge")
    op.drop_table("graph_node")
