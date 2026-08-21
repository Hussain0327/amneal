"""ingredient_chemistry: PubChem identity per active-ingredient name

One additive table (DECISIONS.md 2026-08-21). It is written only by the
operator-run ``regwatch chemistry-backfill`` and read by
``GET /chemistry/structures``; the Ask surface draws the structure figure
from ``smiles`` in the browser. No model ever writes to it.

New and empty: creating it takes only brief metadata locks, nothing
references it, downgrade drops it cleanly. It does not enter the Go sqlc
schema snapshot. The 0011 event trigger auto-applies deny-all RLS at CREATE
time. Postgres-gated like 0019: fresh Postgres bootstraps this table via
create_all (the SQLModel class in store/models.py) and never replays this
file.

Revision ID: 0026_ingredient_chemistry
Revises: 0025_fda_terminal_resolution
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_ingredient_chemistry"
down_revision: str | None = "0025_fda_terminal_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "10s"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")

    op.create_table(
        "ingredient_chemistry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ingredient_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pubchem_cid", sa.Integer(), nullable=True),
        sa.Column("smiles", sa.String(), nullable=True),
        sa.Column("inchikey", sa.String(), nullable=True),
        sa.Column("molecular_formula", sa.String(), nullable=True),
        sa.Column("molecular_weight", sa.Float(), nullable=True),
        sa.Column("iupac_name", sa.String(), nullable=True),
        sa.Column("unii", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('resolved', 'ambiguous', 'not_found')",
            name="ck_ingredient_chemistry_status",
        ),
    )
    op.create_index(
        "ix_ingredient_chemistry_ingredient_key",
        "ingredient_chemistry",
        ["ingredient_key"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_index("ix_ingredient_chemistry_ingredient_key", table_name="ingredient_chemistry")
    op.drop_table("ingredient_chemistry")
