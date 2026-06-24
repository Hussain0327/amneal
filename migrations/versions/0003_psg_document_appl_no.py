"""add appl_no identity column to psg_document

A PSG is uniquely identified by its FDA application number (the ``appl_no`` in
``PSG_<appl_no>.pdf``). Before this, ``psg_document`` was upserted on
(normalized_name, dosage_form, route, rld_or_rs_number), which collides two
distinct PSGs that share those columns (e.g. beclomethasone 020503 vs 020911)
into one row and breaks the version chain. This adds the canonical key.

Revision ID: 0003_psg_document_appl_no
Revises: 0002_chat_sessions
Create Date: 2026-06-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_psg_document_appl_no"
down_revision: str | None = "0002_chat_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("psg_document", sa.Column("appl_no", sa.String(), nullable=True))
    # Backfill pre-existing rows from the canonical PDF URL (``PSG_<appl_no>.pdf``).
    # Rows whose source_url does not match stay NULL (a unique index permits many
    # NULLs in SQLite). A unique-constraint failure on the index below would mean
    # pre-existing duplicate PSG rows for one appl_no -- rebuild the dev DB.
    #
    # WHY the dialect guard: this UPDATE uses SQLite's instr()/substr(), which do
    # not exist in Postgres (instr() would raise UndefinedFunction). On Postgres
    # the backfill is intentionally SKIPPED, leaving the new column NULL. That is
    # consistent with the only path that reaches Postgres here: an incremental
    # `alembic upgrade head` (the Fly release_command) replaying 0003 on a DB
    # stamped behind. A fresh Postgres never replays this migration -- it
    # bootstraps via create_all + stamp head -- so there are no pre-existing rows
    # to backfill there either. Ingestion sets appl_no on subsequent upserts.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""
            UPDATE psg_document
            SET appl_no = substr(
                source_url,
                instr(source_url, 'PSG_') + 4,
                instr(source_url, '.pdf') - instr(source_url, 'PSG_') - 4
            )
            WHERE appl_no IS NULL
              AND instr(source_url, 'PSG_') > 0
              AND instr(source_url, '.pdf') > instr(source_url, 'PSG_')
            """)
    op.create_index("ix_psg_document_appl_no", "psg_document", ["appl_no"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_psg_document_appl_no", table_name="psg_document")
    op.drop_column("psg_document", "appl_no")
