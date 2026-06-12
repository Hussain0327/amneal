"""query_log token/cost accounting + answer_feedback (H3 + H4)

``query_log`` gains nullable ``input_tokens`` / ``output_tokens`` /
``cost_usd`` for the synthesizer call (NULL = no LLM call, unreported usage,
or no configured price — never a guess). ``answer_feedback`` stores one
thumbs up/down per (audit_id, user_id); re-rating replaces.

Dialect notes: the ``query_log`` columns go through ``batch_alter_table`` so
the same migration applies on SQLite (table-recreate batch mode) AND on an
existing Postgres via ``alembic upgrade head``. A FRESH Postgres never replays
this file — its bootstrap is ``create_all`` + ``stamp head``, and the model
metadata (models.py) declares the same columns/constraints, so both paths
produce the same schema.

Revision ID: 0008_token_cost_feedback
Revises: 0007_chat_session_user_updated
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_token_cost_feedback"
down_revision: str | None = "0007_chat_session_user_updated"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("query_log") as batch:
        batch.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cost_usd", sa.Float(), nullable=True))

    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("audit_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["audit_id"], ["query_log.id"]),
        sa.UniqueConstraint("audit_id", "user_id", name="uq_answer_feedback_audit_user"),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_answer_feedback_rating"),
    )
    op.create_index("ix_answer_feedback_audit_id", "answer_feedback", ["audit_id"])
    op.create_index("ix_answer_feedback_user_id", "answer_feedback", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_answer_feedback_user_id", table_name="answer_feedback")
    op.drop_index("ix_answer_feedback_audit_id", table_name="answer_feedback")
    op.drop_table("answer_feedback")

    with op.batch_alter_table("query_log") as batch:
        batch.drop_column("cost_usd")
        batch.drop_column("output_tokens")
        batch.drop_column("input_tokens")
