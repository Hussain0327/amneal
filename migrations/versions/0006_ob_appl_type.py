"""application-type column on ob_patent / ob_exclusivity (typed replace-snapshots)

NDA and ANDA applications sharing the same six digits are DIFFERENT
applications. The White-Paper replace-snapshot previously deleted by
``appl_no`` alone, so persisting one type's snapshot silently wiped the
other's durable patent/exclusivity provenance rows (a cross-type wipe).

This revision adds a nullable, indexed ``appl_type`` to ``ob_patent`` and
``ob_exclusivity`` (``ob_product`` already carries one) and normalizes the
legacy Orange Book single-letter values on ``ob_product`` (``N``/``A``/``B``)
to the full ``NDA``/``ANDA``/``BLA`` prefixes the typed delete filters on.
Rows that remain NULL are pre-0006 legacy rows; the first typed replace for
their application number retires them.

Revision ID: 0006_ob_appl_type
Revises: 0005_whitepaper_sources
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_ob_appl_type"
down_revision: str | None = "0005_whitepaper_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LETTER_TO_FULL = (("N", "NDA"), ("A", "ANDA"), ("B", "BLA"))


def upgrade() -> None:
    op.add_column("ob_patent", sa.Column("appl_type", sa.String(), nullable=True))
    op.create_index("ix_ob_patent_appl_type", "ob_patent", ["appl_type"])

    op.add_column("ob_exclusivity", sa.Column("appl_type", sa.String(), nullable=True))
    op.create_index("ix_ob_exclusivity_appl_type", "ob_exclusivity", ["appl_type"])

    for letter, full in _LETTER_TO_FULL:
        op.execute(f"UPDATE ob_product SET appl_type = '{full}' WHERE appl_type = '{letter}'")


def downgrade() -> None:
    for letter, full in _LETTER_TO_FULL:
        op.execute(f"UPDATE ob_product SET appl_type = '{letter}' WHERE appl_type = '{full}'")

    op.drop_index("ix_ob_exclusivity_appl_type", table_name="ob_exclusivity")
    op.drop_column("ob_exclusivity", "appl_type")

    op.drop_index("ix_ob_patent_appl_type", table_name="ob_patent")
    op.drop_column("ob_patent", "appl_type")
