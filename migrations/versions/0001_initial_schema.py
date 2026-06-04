"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active_ingredient", sa.String(), nullable=False),
        sa.Column("normalized_name", sa.String(), nullable=False),
        sa.Column("dosage_form", sa.String(), nullable=True),
        sa.Column("route", sa.String(), nullable=True),
        sa.Column("rld_name", sa.String(), nullable=True),
        sa.Column("rld_application_number", sa.String(), nullable=True),
        sa.Column("company_status", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("on_watchlist", sa.Boolean(), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_normalized_name", "product", ["normalized_name"])

    op.create_table(
        "psg_document",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active_ingredient", sa.String(), nullable=False),
        sa.Column("normalized_name", sa.String(), nullable=False),
        sa.Column("dosage_form", sa.String(), nullable=True),
        sa.Column("route", sa.String(), nullable=True),
        sa.Column("rld_or_rs_number", sa.String(), nullable=True),
        sa.Column("psg_type", sa.String(), nullable=False),
        sa.Column("recommended_date", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("pdf_path", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_psg_document_content_hash", "psg_document", ["content_hash"])
    op.create_index("ix_psg_document_normalized_name", "psg_document", ["normalized_name"])

    op.create_table(
        "psg_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("psg_document_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("recommended_date", sa.String(), nullable=True),
        sa.Column("parsed_text_path", sa.String(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("diff_summary", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["psg_document_id"], ["psg_document.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_psg_version_content_hash", "psg_version", ["content_hash"])
    op.create_index("ix_psg_version_psg_document_id", "psg_version", ["psg_document_id"])

    op.create_table(
        "be_requirement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("psg_document_id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("study_type", sa.String(), nullable=True),
        sa.Column("study_design", sa.String(), nullable=True),
        sa.Column("strengths", sa.String(), nullable=True),
        sa.Column("dissolution", sa.String(), nullable=True),
        sa.Column("waiver_conditions", sa.String(), nullable=True),
        sa.Column("additional_notes", sa.String(), nullable=True),
        sa.Column("fields_json", sa.JSON(), nullable=True),
        sa.Column("citations_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["psg_document_id"], ["psg_document.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["psg_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_be_requirement_psg_document_id", "be_requirement", ["psg_document_id"])
    op.create_index("ix_be_requirement_version_id", "be_requirement", ["version_id"])

    op.create_table(
        "query_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("query_text", sa.String(), nullable=False),
        sa.Column("retrieved_json", sa.JSON(), nullable=True),
        sa.Column("answer_text", sa.String(), nullable=False),
        sa.Column("citations_json", sa.JSON(), nullable=True),
        sa.Column("refused", sa.Boolean(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_query_log_ts", "query_log", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_query_log_ts", table_name="query_log")
    op.drop_table("query_log")
    op.drop_index("ix_be_requirement_version_id", table_name="be_requirement")
    op.drop_index("ix_be_requirement_psg_document_id", table_name="be_requirement")
    op.drop_table("be_requirement")
    op.drop_index("ix_psg_version_psg_document_id", table_name="psg_version")
    op.drop_index("ix_psg_version_content_hash", table_name="psg_version")
    op.drop_table("psg_version")
    op.drop_index("ix_psg_document_normalized_name", table_name="psg_document")
    op.drop_index("ix_psg_document_content_hash", table_name="psg_document")
    op.drop_table("psg_document")
    op.drop_index("ix_product_normalized_name", table_name="product")
    op.drop_table("product")
