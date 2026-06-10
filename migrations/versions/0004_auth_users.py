"""auth users, db-backed login sessions, and query_log user attribution

Cookie-session auth for the pilot: ``user`` holds CLI-provisioned accounts
(bcrypt hashes), ``auth_session`` holds server-side sessions keyed by the
sha256 of the opaque cookie token (the raw token is never stored). Audit rows
(INV-6) gain a nullable ``user_id`` so every authenticated query/assemble
carries caller identity.

Revision ID: 0004_auth_users
Revises: 0003_psg_document_appl_no
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_auth_users"
down_revision: str | None = "0003_psg_document_appl_no"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_email", "user", ["email"], unique=True)

    op.create_table(
        "auth_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_session_token_hash", "auth_session", ["token_hash"], unique=True)
    op.create_index("ix_auth_session_user_id", "auth_session", ["user_id"])

    op.add_column("query_log", sa.Column("user_id", sa.String(), nullable=True))
    op.create_index("ix_query_log_user_id", "query_log", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_query_log_user_id", table_name="query_log")
    op.drop_column("query_log", "user_id")

    op.drop_index("ix_auth_session_user_id", table_name="auth_session")
    op.drop_index("ix_auth_session_token_hash", table_name="auth_session")
    op.drop_table("auth_session")

    op.drop_index("ix_user_email", table_name="user")
    op.drop_table("user")
