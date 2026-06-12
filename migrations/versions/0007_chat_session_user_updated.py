"""composite (user_id, updated_at) index on chat_session

GET /sessions orders a user's sessions by ``updated_at``; the composite index
keeps that page a single index scan on hosted Postgres. The index is declared
on the ``ChatSession`` model (``__table_args__``) so ``create_all`` (the
Postgres bootstrap) and alembic autogenerate both see it; this migration
brings existing SQLite databases up to the same schema.

``if_not_exists``: databases that booted an earlier build of this branch
already carry the index from (since-removed) ad-hoc bootstrap DDL, so the
upgrade must tolerate it.

Revision ID: 0007_chat_session_user_updated
Revises: 0006_ob_appl_type
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_chat_session_user_updated"
down_revision: str | None = "0006_ob_appl_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_chat_session_user_id_updated_at",
        "chat_session",
        ["user_id", "updated_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_session_user_id_updated_at",
        table_name="chat_session",
        if_exists=True,
    )
