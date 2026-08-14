"""streaming artifact, chunk, embedding, and manifest lifecycle

Revision ID: 0024_fda_streaming_lifecycle
Revises: 0023_authoritative_fda_corpus
Create Date: 2026-08-13

This migration is data-local and deterministic. It records the boundaries a
document-at-a-time worker needs to resume safely; it never downloads, parses,
chunks, or embeds FDA content during deployment.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_fda_streaming_lifecycle"
down_revision: str | None = "0023_authoritative_fda_corpus"
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

    op.add_column("fda_document", sa.Column("shard_id", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_fda_document_shard_id",
        "fda_document",
        "shard_id IS NULL OR (shard_id >= 0 AND shard_id < 512)",
    )
    op.create_index("ix_fda_document_shard_id", "fda_document", ["shard_id"])

    op.add_column("fda_document_version", sa.Column("artifact_uri", sa.String(), nullable=True))
    op.add_column(
        "fda_document_version",
        sa.Column(
            "artifact_retained",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "fda_document_version",
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "fda_document_version",
        sa.Column("chunk_status", sa.String(), nullable=False, server_default="pending"),
    )
    op.add_column(
        "fda_document_version",
        sa.Column("chunked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("fda_document_version", sa.Column("chunk_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_fda_document_version_chunk_status",
        "fda_document_version",
        "chunk_status IN ('pending', 'complete', 'failed')",
    )
    for column in ("acquired_at", "chunk_status", "chunked_at"):
        op.create_index(
            f"ix_fda_document_version_{column}",
            "fda_document_version",
            [column],
        )

    # Migration 0023 only created a version after all chunks were committed,
    # so every pre-existing indexed version is truthfully complete. Local file
    # paths are preserved for audit but are not claimed as durable artifacts.
    op.execute(
        sa.text(
            "UPDATE fda_document_version v SET "
            "artifact_uri = v.artifact_path, artifact_retained = false, "
            "acquired_at = v.fetched_at, "
            "chunk_status = CASE WHEN EXISTS ("
            "  SELECT 1 FROM chunk c WHERE c.fda_version_id = v.id"
            ") THEN 'complete' ELSE 'failed' END, "
            "chunked_at = CASE WHEN EXISTS ("
            "  SELECT 1 FROM chunk c WHERE c.fda_version_id = v.id"
            ") THEN v.fetched_at ELSE NULL END, "
            "chunk_error = CASE WHEN EXISTS ("
            "  SELECT 1 FROM chunk c WHERE c.fda_version_id = v.id"
            ") THEN NULL ELSE 'pre-0024 version has no indexed chunks' END"
        )
    )

    op.create_table(
        "fda_version_embedding_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "fda_version_id",
            sa.Integer(),
            sa.ForeignKey("fda_document_version.id"),
            nullable=False,
        ),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("expected_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedded_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'failed')",
            name="ck_fda_version_embedding_state_status",
        ),
        sa.CheckConstraint(
            "expected_chunks >= 0",
            name="ck_fda_version_embedding_state_expected_chunks",
        ),
        sa.CheckConstraint(
            "embedded_chunks >= 0 AND embedded_chunks <= expected_chunks",
            name="ck_fda_version_embedding_state_embedded_chunks",
        ),
    )
    op.create_index(
        "uq_fda_version_embedding_state_version_profile",
        "fda_version_embedding_state",
        ["fda_version_id", "profile_id"],
        unique=True,
    )
    for column in ("fda_version_id", "profile_id", "status", "updated_at"):
        op.create_index(
            f"ix_fda_version_embedding_state_{column}",
            "fda_version_embedding_state",
            [column],
        )

    # Capture truthful legacy coverage for versions already committed by 0023.
    op.execute(
        sa.text(
            "INSERT INTO fda_version_embedding_state ("
            "fda_version_id, profile_id, expected_chunks, embedded_chunks, status, "
            "started_at, completed_at, updated_at"
            ") SELECT v.id, 'legacy', v.chunk_count, count(c.embedding), "
            "CASE WHEN count(c.embedding) = v.chunk_count AND v.chunk_count > 0 "
            "THEN 'complete' ELSE 'pending' END, v.fetched_at, "
            "CASE WHEN count(c.embedding) = v.chunk_count AND v.chunk_count > 0 "
            "THEN v.fetched_at ELSE NULL END, now() "
            "FROM fda_document_version v LEFT JOIN chunk c ON c.fda_version_id = v.id "
            "GROUP BY v.id, v.chunk_count, v.fetched_at"
        )
    )
    # Preserve any immutable profile coverage that already exists.
    op.execute(
        sa.text(
            "INSERT INTO fda_version_embedding_state ("
            "fda_version_id, profile_id, expected_chunks, embedded_chunks, status, "
            "started_at, completed_at, updated_at"
            ") SELECT v.id, ce.profile_id, v.chunk_count, count(ce.chunk_id), "
            "CASE WHEN count(ce.chunk_id) = v.chunk_count AND v.chunk_count > 0 "
            "THEN 'complete' ELSE 'pending' END, v.fetched_at, "
            "CASE WHEN count(ce.chunk_id) = v.chunk_count AND v.chunk_count > 0 "
            "THEN v.fetched_at ELSE NULL END, now() "
            "FROM fda_document_version v JOIN chunk c ON c.fda_version_id = v.id "
            "JOIN chunk_embedding ce ON ce.chunk_id = c.id "
            "GROUP BY v.id, ce.profile_id, v.chunk_count, v.fetched_at "
            "ON CONFLICT (fda_version_id, profile_id) DO UPDATE SET "
            "embedded_chunks = EXCLUDED.embedded_chunks, status = EXCLUDED.status, "
            "completed_at = EXCLUDED.completed_at, updated_at = now()"
        )
    )

    op.create_table(
        "fda_corpus_manifest",
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("artifact_uri", sa.String(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_retained", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("complete_universe", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "source_snapshots_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "counts_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "document_count >= 0",
            name="ck_fda_corpus_manifest_document_count",
        ),
    )
    op.create_index("ix_fda_corpus_manifest_created_at", "fda_corpus_manifest", ["created_at"])

    for table in ("fda_version_embedding_state", "fda_corpus_manifest"):
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)

    op.drop_index("ix_fda_corpus_manifest_created_at", table_name="fda_corpus_manifest")
    op.drop_table("fda_corpus_manifest")

    for column in ("updated_at", "status", "profile_id", "fda_version_id"):
        op.drop_index(
            f"ix_fda_version_embedding_state_{column}",
            table_name="fda_version_embedding_state",
        )
    op.drop_index(
        "uq_fda_version_embedding_state_version_profile",
        table_name="fda_version_embedding_state",
    )
    op.drop_table("fda_version_embedding_state")

    for column in ("chunked_at", "chunk_status", "acquired_at"):
        op.drop_index(f"ix_fda_document_version_{column}", table_name="fda_document_version")
    op.drop_constraint(
        "ck_fda_document_version_chunk_status",
        "fda_document_version",
        type_="check",
    )
    for column in (
        "chunk_error",
        "chunked_at",
        "chunk_status",
        "acquired_at",
        "artifact_retained",
        "artifact_uri",
    ):
        op.drop_column("fda_document_version", column)

    op.drop_index("ix_fda_document_shard_id", table_name="fda_document")
    op.drop_constraint("ck_fda_document_shard_id", "fda_document", type_="check")
    op.drop_column("fda_document", "shard_id")
