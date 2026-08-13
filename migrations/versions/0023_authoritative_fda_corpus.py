"""authoritative FDA corpus document, version, run, and chunk provenance

Revision ID: 0023_authoritative_fda_corpus
Revises: 0022_deficiency_run_source
Create Date: 2026-08-13

The old corpus model made a PSG row the document identity.  This migration
adds a source-neutral FDA document/version ledger and links current-search
chunks to it.  The legacy PSG keys stay nullable and intact so the migration
does not rewrite or invalidate the serving corpus.

No data backfill runs inside this migration.  Fetching, parsing, and embedding
FDA artifacts can take hours and make network calls; those operations belong
to the resumable corpus sync command, never to schema deployment.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_authoritative_fda_corpus"
down_revision: str | None = "0022_deficiency_run_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "10s"
_SOURCE_FAMILIES = (
    "drugs_at_fda",
    "action_package",
    "psg",
    "fda_be_guidance",
    "orange_book",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _bounded_lock(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)

    op.create_table(
        "fda_document",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("canonical_id", sa.String(), nullable=False),
        sa.Column("source_family", sa.String(), nullable=False),
        sa.Column("document_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("application_number", sa.String(), nullable=True),
        sa.Column("product_number", sa.String(), nullable=True),
        sa.Column("active_ingredient", sa.String(), nullable=True),
        sa.Column("normalized_name", sa.String(), nullable=True),
        sa.Column("brand_name", sa.String(), nullable=True),
        sa.Column("dosage_form", sa.String(), nullable=True),
        sa.Column("route", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"source_family IN ({_quoted(_SOURCE_FAMILIES)})",
            name="ck_fda_document_source_family",
        ),
    )
    op.create_index("ix_fda_document_canonical_id", "fda_document", ["canonical_id"], unique=True)
    for column in (
        "source_family",
        "document_type",
        "application_number",
        "product_number",
        "normalized_name",
        "is_active",
        "last_seen_at",
    ):
        op.create_index(f"ix_fda_document_{column}", "fda_document", [column])

    op.create_table(
        "fda_document_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "fda_document_id",
            sa.Integer(),
            sa.ForeignKey("fda_document.id"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("processing_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_updated_at", sa.String(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("artifact_path", sa.String(), nullable=True),
        sa.Column("parse_engine", sa.String(), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint("byte_size >= 0", name="ck_fda_document_version_byte_size"),
        sa.CheckConstraint("page_count >= 0", name="ck_fda_document_version_page_count"),
        sa.CheckConstraint("chunk_count >= 0", name="ck_fda_document_version_chunk_count"),
    )
    op.create_index(
        "uq_fda_document_version_doc_hash",
        "fda_document_version",
        ["fda_document_id", "content_hash", "processing_fingerprint"],
        unique=True,
    )
    for column in (
        "fda_document_id",
        "content_hash",
        "processing_fingerprint",
        "fetched_at",
    ):
        op.create_index(
            f"ix_fda_document_version_{column}",
            "fda_document_version",
            [column],
        )

    op.create_table(
        "fda_corpus_run",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_documents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discovered_documents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("added_documents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revised_documents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged_documents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_documents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunks_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "stats_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint("mode IN ('plan', 'sync')", name="ck_fda_corpus_run_mode"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_fda_corpus_run_status",
        ),
    )
    op.create_index("ix_fda_corpus_run_status", "fda_corpus_run", ["status"])
    op.create_index("ix_fda_corpus_run_started_at", "fda_corpus_run", ["started_at"])

    op.add_column("chunk", sa.Column("fda_document_id", sa.Integer(), nullable=True))
    op.add_column("chunk", sa.Column("fda_version_id", sa.Integer(), nullable=True))
    op.add_column("chunk", sa.Column("source_family", sa.String(), nullable=True))
    op.add_column("chunk", sa.Column("document_type", sa.String(), nullable=True))
    op.add_column("chunk", sa.Column("locator", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_chunk_fda_document_id",
        "chunk",
        "fda_document",
        ["fda_document_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_chunk_fda_version_id",
        "chunk",
        "fda_document_version",
        ["fda_version_id"],
        ["id"],
    )
    for column in (
        "fda_document_id",
        "fda_version_id",
        "source_family",
        "document_type",
    ):
        op.create_index(f"ix_chunk_{column}", "chunk", [column])

    # Same deny-by-default posture as every other public table.  Migration
    # 0011's event trigger also applies this, but explicit DDL keeps this
    # migration correct when the trigger is unavailable on a managed service.
    for table in ("fda_document", "fda_document_version", "fda_corpus_run"):
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)

    for column in (
        "document_type",
        "source_family",
        "fda_version_id",
        "fda_document_id",
    ):
        op.drop_index(f"ix_chunk_{column}", table_name="chunk")
    # A migration-replayed DB has our explicit names; fresh create_all + stamp
    # uses PostgreSQL's conventional names.  Downgrade must work for both
    # bootstrap routes (the repository's convergence contract).
    for constraint in (
        "fk_chunk_fda_version_id",
        "chunk_fda_version_id_fkey",
        "fk_chunk_fda_document_id",
        "chunk_fda_document_id_fkey",
    ):
        op.execute(sa.text(f'ALTER TABLE chunk DROP CONSTRAINT IF EXISTS "{constraint}"'))
    for column in (
        "locator",
        "document_type",
        "source_family",
        "fda_version_id",
        "fda_document_id",
    ):
        op.drop_column("chunk", column)

    op.drop_index("ix_fda_corpus_run_started_at", table_name="fda_corpus_run")
    op.drop_index("ix_fda_corpus_run_status", table_name="fda_corpus_run")
    op.drop_table("fda_corpus_run")

    for column in (
        "fetched_at",
        "processing_fingerprint",
        "content_hash",
        "fda_document_id",
    ):
        op.drop_index(f"ix_fda_document_version_{column}", table_name="fda_document_version")
    op.drop_index("uq_fda_document_version_doc_hash", table_name="fda_document_version")
    op.drop_table("fda_document_version")

    for column in (
        "last_seen_at",
        "is_active",
        "normalized_name",
        "product_number",
        "application_number",
        "document_type",
        "source_family",
    ):
        op.drop_index(f"ix_fda_document_{column}", table_name="fda_document")
    op.drop_index("ix_fda_document_canonical_id", table_name="fda_document")
    op.drop_table("fda_document")
