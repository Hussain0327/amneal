"""chat sessions and conversational audit fields

Revision ID: 0002_chat_sessions
Revises: 0001_initial_schema
Create Date: 2026-06-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_chat_sessions"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_session",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("active_filters_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_session_user_id", "chat_session", ["user_id"])
    op.create_index("ix_chat_session_created_at", "chat_session", ["created_at"])
    op.create_index("ix_chat_session_updated_at", "chat_session", ["updated_at"])

    op.create_table(
        "chat_message",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("model_name", sa.String(), nullable=True),
        sa.Column("audit_id", sa.Integer(), nullable=True),
        sa.Column("filters_json", sa.JSON(), nullable=True),
        sa.Column("citations_json", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_session.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_message_session_id", "chat_message", ["session_id"])
    op.create_index("ix_chat_message_turn_id", "chat_message", ["turn_id"])
    op.create_index("ix_chat_message_audit_id", "chat_message", ["audit_id"])
    op.create_index("ix_chat_message_created_at", "chat_message", ["created_at"])

    op.add_column("query_log", sa.Column("session_id", sa.String(), nullable=True))
    op.add_column("query_log", sa.Column("turn_id", sa.String(), nullable=True))
    op.add_column("query_log", sa.Column("status", sa.String(), nullable=True))
    op.add_column("query_log", sa.Column("route_json", sa.JSON(), nullable=True))
    op.create_index("ix_query_log_session_id", "query_log", ["session_id"])
    op.create_index("ix_query_log_turn_id", "query_log", ["turn_id"])


def downgrade() -> None:
    op.drop_index("ix_query_log_turn_id", table_name="query_log")
    op.drop_index("ix_query_log_session_id", table_name="query_log")
    op.drop_column("query_log", "route_json")
    op.drop_column("query_log", "status")
    op.drop_column("query_log", "turn_id")
    op.drop_column("query_log", "session_id")

    op.drop_index("ix_chat_message_created_at", table_name="chat_message")
    op.drop_index("ix_chat_message_audit_id", table_name="chat_message")
    op.drop_index("ix_chat_message_turn_id", table_name="chat_message")
    op.drop_index("ix_chat_message_session_id", table_name="chat_message")
    op.drop_table("chat_message")

    op.drop_index("ix_chat_session_updated_at", table_name="chat_session")
    op.drop_index("ix_chat_session_created_at", table_name="chat_session")
    op.drop_index("ix_chat_session_user_id", table_name="chat_session")
    op.drop_table("chat_session")
