"""chat_message provenance + next-step affordances (Ask Tier-2)

Persists per-turn provenance and affordances so a rehydrated conversation keeps
them: ``reason`` / ``interpretation`` (WHY we answered/declined/clarified) and
``clarify_json`` / ``related_json`` (re-runnable next-step options). ``audit_id``
already exists (migration 0002); only these four are new.

All four are NULLABLE with NO server default, so on Postgres each
``ADD COLUMN`` is a metadata-only catalog change — no table rewrite, no long
``ACCESS EXCLUSIVE`` hold on the live ``chat_message`` table (the 2026-06-18
lock-pileup incident class). The columns go through ``batch_alter_table`` so the
same migration also applies on SQLite (table-recreate batch mode). A FRESH
Postgres never replays this file — its bootstrap is ``create_all`` + ``stamp
head``, and ``models.py``'s ``ChatMessage`` now declares the same columns, so
both paths produce the same schema. Fully reversible via ``downgrade``.

Revision ID: 0010_chat_message_provenance
Revises: 0009_durable_alerts
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_chat_message_provenance"
down_revision: str | None = "0009_durable_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("chat_message") as batch:
        batch.add_column(sa.Column("reason", sa.String(), nullable=True))
        batch.add_column(sa.Column("interpretation", sa.String(), nullable=True))
        batch.add_column(sa.Column("clarify_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("related_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chat_message") as batch:
        batch.drop_column("related_json")
        batch.drop_column("clarify_json")
        batch.drop_column("interpretation")
        batch.drop_column("reason")
