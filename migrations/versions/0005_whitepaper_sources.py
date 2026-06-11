"""white-paper structured-source persistence (ob_product/patent/exclusivity, spl_document)

The CRA White-Paper populator (Gate 2) writes-through Orange Book product /
patent / exclusivity rows and the DailyMed SPL resolution it fetches, so each
populated cell can cite a durable row carrying its ``last_fetched_at`` freshness
(INV-5). Raw rows only — no paragraph classification or eligibility (INV-3).

Revision ID: 0005_whitepaper_sources
Revises: 0004_auth_users
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_whitepaper_sources"
down_revision: str | None = "0004_auth_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ob_product",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appl_no", sa.String(), nullable=False),
        sa.Column("product_no", sa.String(), nullable=False),
        sa.Column("appl_type", sa.String(), nullable=True),
        sa.Column("ingredient", sa.String(), nullable=True),
        sa.Column("normalized_name", sa.String(), nullable=True),
        sa.Column("trade_name", sa.String(), nullable=True),
        sa.Column("dosage_form_route", sa.String(), nullable=True),
        sa.Column("strength", sa.String(), nullable=True),
        sa.Column("rld", sa.String(), nullable=True),
        sa.Column("rs", sa.String(), nullable=True),
        sa.Column("te_code", sa.String(), nullable=True),
        sa.Column("approval_date", sa.String(), nullable=True),
        sa.Column("applicant", sa.String(), nullable=True),
        sa.Column("applicant_full_name", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ob_product_appl_no", "ob_product", ["appl_no"])
    op.create_index("ix_ob_product_product_no", "ob_product", ["product_no"])
    op.create_index("ix_ob_product_normalized_name", "ob_product", ["normalized_name"])

    op.create_table(
        "ob_patent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appl_no", sa.String(), nullable=False),
        sa.Column("product_no", sa.String(), nullable=True),
        sa.Column("patent_no", sa.String(), nullable=False),
        sa.Column("patent_expire_date", sa.String(), nullable=True),
        sa.Column("drug_substance_flag", sa.String(), nullable=True),
        sa.Column("drug_product_flag", sa.String(), nullable=True),
        sa.Column("patent_use_code", sa.String(), nullable=True),
        sa.Column("delist_flag", sa.String(), nullable=True),
        sa.Column("submission_date", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ob_patent_appl_no", "ob_patent", ["appl_no"])
    op.create_index("ix_ob_patent_patent_no", "ob_patent", ["patent_no"])

    op.create_table(
        "ob_exclusivity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appl_no", sa.String(), nullable=False),
        sa.Column("product_no", sa.String(), nullable=True),
        sa.Column("exclusivity_code", sa.String(), nullable=False),
        sa.Column("exclusivity_date", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ob_exclusivity_appl_no", "ob_exclusivity", ["appl_no"])
    op.create_index("ix_ob_exclusivity_exclusivity_code", "ob_exclusivity", ["exclusivity_code"])

    op.create_table(
        "spl_document",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("setid", sa.String(), nullable=False),
        sa.Column("appl_no", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("published", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_spl_document_setid", "spl_document", ["setid"], unique=True)
    op.create_index("ix_spl_document_appl_no", "spl_document", ["appl_no"])


def downgrade() -> None:
    op.drop_index("ix_spl_document_appl_no", table_name="spl_document")
    op.drop_index("ix_spl_document_setid", table_name="spl_document")
    op.drop_table("spl_document")

    op.drop_index("ix_ob_exclusivity_exclusivity_code", table_name="ob_exclusivity")
    op.drop_index("ix_ob_exclusivity_appl_no", table_name="ob_exclusivity")
    op.drop_table("ob_exclusivity")

    op.drop_index("ix_ob_patent_patent_no", table_name="ob_patent")
    op.drop_index("ix_ob_patent_appl_no", table_name="ob_patent")
    op.drop_table("ob_patent")

    op.drop_index("ix_ob_product_normalized_name", table_name="ob_product")
    op.drop_index("ix_ob_product_product_no", table_name="ob_product")
    op.drop_index("ix_ob_product_appl_no", table_name="ob_product")
    op.drop_table("ob_product")
