"""durable watch alerts (P0)

Persists Watch alerts in Postgres so the feed survives Fly redeploys — the
on-disk JSONL digest is ephemeral (no [[mounts]] on Fly). Pure
``create_table`` + ``create_index``: dialect-portable, so it applies to the
live Supabase Postgres via ``alembic upgrade head`` AND to SQLite via
``_init_sqlite``'s ``command.upgrade``. A FRESH Postgres never replays this
file — its bootstrap is ``create_all`` + ``stamp head``, and ``models.py``'s
``Alert`` declares the same columns/constraints/indexes, so both paths produce
the same schema.

Revision ID: 0009_durable_alerts
Revises: 0008_token_cost_feedback
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_durable_alerts"
down_revision: str | None = "0008_token_cost_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("active_ingredient", sa.String(), nullable=False),
        sa.Column("listing_appl_no", sa.String(), nullable=False),
        sa.Column("listing_psg_type", sa.String(), nullable=False),
        sa.Column("psg_document_id", sa.Integer(), nullable=False),
        sa.Column("psg_version_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.String(), nullable=False),
        sa.Column("diff_summary", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "psg_version_id",
            "listing_appl_no",
            "product_id",
            name="uq_alert_version_listing_product",
        ),
    )
    op.create_index("ix_alert_product_id", "alert", ["product_id"])
    op.create_index("ix_alert_listing_appl_no", "alert", ["listing_appl_no"])
    op.create_index("ix_alert_psg_document_id", "alert", ["psg_document_id"])
    op.create_index("ix_alert_psg_version_id", "alert", ["psg_version_id"])
    op.create_index("ix_alert_created_at", "alert", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_alert_created_at", table_name="alert")
    op.drop_index("ix_alert_psg_version_id", table_name="alert")
    op.drop_index("ix_alert_psg_document_id", table_name="alert")
    op.drop_index("ix_alert_listing_appl_no", table_name="alert")
    op.drop_index("ix_alert_product_id", table_name="alert")
    op.drop_table("alert")
