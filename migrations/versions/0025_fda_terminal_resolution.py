"""audited terminal resolution ledger for FDA manifest records

Revision ID: 0025_fda_terminal_resolution
Revises: 0024_fda_streaming_lifecycle
Create Date: 2026-08-17

The migration is data-local.  It identifies the version already supplying each
document's current chunks, but it never fetches a source, classifies an old
failure as terminal, or changes retrieval configuration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_fda_terminal_resolution"
down_revision: str | None = "0024_fda_streaming_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "10s"


def _bounded_lock(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)

    op.add_column(
        "fda_document_version",
        sa.Column(
            "content_hash_kind",
            sa.String(),
            nullable=False,
            server_default="source_bytes",
        ),
    )
    op.add_column(
        "fda_document_version",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "fda_document_version",
        sa.Column(
            "resolution_status",
            sa.String(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "fda_document_version",
        sa.Column(
            "resolution_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "fda_document_version",
        sa.Column("last_resolution_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "fda_document_version",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "fda_document_version",
        sa.Column("resolution_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "fda_document_version",
        sa.Column(
            "resolution_evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # Migration 0024's atomic publisher leaves chunks for exactly one version
    # per document.  Prefer that searchable version; documents with no chunks
    # use their newest acquired lifecycle row.  No old failure becomes terminal
    # merely because this migration was deployed.
    op.execute(
        sa.text(
            "UPDATE fda_document_version SET "
            "resolution_status = 'indexed', "
            "resolved_at = COALESCE(chunked_at, fetched_at) "
            "WHERE chunk_status = 'complete' AND chunk_count > 0 "
            "AND EXISTS (SELECT 1 FROM chunk c "
            "            WHERE c.fda_version_id = fda_document_version.id)"
        )
    )
    op.execute(
        sa.text(
            "WITH ranked AS ("
            "  SELECT v.id, row_number() OVER ("
            "    PARTITION BY v.fda_document_id ORDER BY "
            "    CASE WHEN EXISTS (SELECT 1 FROM chunk c "
            "                     WHERE c.fda_version_id = v.id) "
            "         THEN 0 ELSE 1 END, "
            "    COALESCE(v.chunked_at, v.acquired_at, v.fetched_at) DESC, "
            "    v.id DESC"
            "  ) AS position "
            "  FROM fda_document_version v"
            ") UPDATE fda_document_version v SET is_current = true "
            "FROM ranked r WHERE v.id = r.id AND r.position = 1"
        )
    )

    op.create_check_constraint(
        "ck_fda_document_version_content_hash_kind",
        "fda_document_version",
        "content_hash_kind IN ('source_bytes', 'terminal_observation')",
    )
    op.create_check_constraint(
        "ck_fda_document_version_resolution_status",
        "fda_document_version",
        "resolution_status IN ('pending', 'indexed', " "'missing_at_source', 'unparseable')",
    )
    op.create_check_constraint(
        "ck_fda_document_version_resolution_attempts",
        "fda_document_version",
        "resolution_attempts >= 0",
    )
    op.create_check_constraint(
        "ck_fda_document_version_resolution_lifecycle",
        "fda_document_version",
        "(resolution_status = 'pending') OR "
        "(resolution_status = 'indexed' AND chunk_status = 'complete' "
        "AND chunk_count > 0 AND resolved_at IS NOT NULL) OR "
        "(resolution_status IN ('missing_at_source', 'unparseable') "
        "AND chunk_status = 'failed' AND chunk_count = 0 "
        "AND resolved_at IS NOT NULL)",
    )
    op.create_index(
        "ix_fda_document_version_resolution_status",
        "fda_document_version",
        ["resolution_status"],
    )
    op.create_index(
        "uq_fda_document_version_current",
        "fda_document_version",
        ["fda_document_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.add_column(
        "fda_corpus_run",
        sa.Column(
            "terminal_documents",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_fda_corpus_run_terminal_documents",
        "fda_corpus_run",
        "terminal_documents >= 0",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)

    op.drop_constraint(
        "ck_fda_corpus_run_terminal_documents",
        "fda_corpus_run",
        type_="check",
    )
    op.drop_column("fda_corpus_run", "terminal_documents")

    op.drop_index(
        "uq_fda_document_version_current",
        table_name="fda_document_version",
    )
    op.drop_index(
        "ix_fda_document_version_resolution_status",
        table_name="fda_document_version",
    )
    for constraint in (
        "ck_fda_document_version_resolution_lifecycle",
        "ck_fda_document_version_resolution_attempts",
        "ck_fda_document_version_resolution_status",
        "ck_fda_document_version_content_hash_kind",
    ):
        op.drop_constraint(constraint, "fda_document_version", type_="check")
    for column in (
        "resolution_evidence_json",
        "resolution_error",
        "resolved_at",
        "last_resolution_attempt_at",
        "resolution_attempts",
        "resolution_status",
        "is_current",
        "content_hash_kind",
    ):
        op.drop_column("fda_document_version", column)
