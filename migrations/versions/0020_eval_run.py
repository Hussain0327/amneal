"""durable eval_run ledger so scorecards are comparable across runs

Until now an eval scorecard existed only as terminal output plus an optional
``--out`` JSON file. Neither survives: the terminal scrolls, and the artifact
lives on a CI run that ages out. Comparing "before and after a chunker change"
meant comparing a number in a PR comment to a number in someone's memory.

This is the same ledger idea as ``watch_run`` (0012) and ``deficiency_run``
(0019): one durable row per run, recording what was measured AND the
configuration it was measured under, so a later regression can be traced to the
run that introduced it.

Deliberately ONE table, not a run/item pair. The per-question traces are
already carried in ``artifact_json`` and are read by humans debugging a single
regression, not aggregated in SQL -- a second table would add a join and a
foreign key for a query nobody runs.

``gold_set_sha256`` is load-bearing: two runs over different gold sets are not
comparable, and without the hash that difference is invisible in a trend chart.

Note this does NOT adopt the ad-hoc ``eval`` schema (eval.gold / eval.run /
eval.corpus) found on the Lakebase staging database. Those were created by hand
during the 2026-07 embedding benchmark, are not managed by any migration, exist
on exactly one database, and encode a different evaluation model
(``expected_ids`` as chunk ids) than the repo's gold set, which pins
``(short_name, page)`` and carries refuse/clarify expectations. Forcing this
model into that shape would lose information.

Revision ID: 0020_eval_run
Revises: 0019_deficiency
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_eval_run"
down_revision: str | None = "0019_deficiency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "10s"


def _bounded_lock(bind: sa.engine.Connection) -> None:
    """Apply the lock timeout on Postgres; a no-op on other backends."""
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)
    # CREATE TABLE takes no lock on anything that already exists, so this
    # cannot queue behind live readers the way an ALTER on `chunk` can.
    op.create_table(
        "eval_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # 'legacy' or a registered ep_ profile id. Not an FK: a run against a
        # profile that is later deleted is still a true historical record, and
        # ON DELETE RESTRICT would make the ledger block profile cleanup.
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        # A dirty tree means the run is not reproducible from its commit alone,
        # which is exactly the run you must not trust in a comparison.
        sa.Column("dirty", sa.Boolean(), nullable=False),
        sa.Column("gold_set_sha256", sa.String(64), nullable=False),
        sa.Column("n_items", sa.Integer(), nullable=False),
        sa.Column("corpus_chunks", sa.Integer(), nullable=False),
        sa.Column("corpus_docs", sa.Integer(), nullable=False),
        # Float, not Numeric: these are means of bounded ratios, consumed as
        # trend lines. Exact decimal semantics buy nothing here.
        sa.Column("recall_at_k", sa.Float(), nullable=False),
        sa.Column("mrr", sa.Float(), nullable=False),
        sa.Column("citation_precision", sa.Float(), nullable=False),
        sa.Column("faithfulness", sa.Float(), nullable=False),
        sa.Column("fact_recall", sa.Float(), nullable=False),
        sa.Column("refusal_accuracy", sa.Float(), nullable=False),
        # Whether this run cleared the gate. Stored rather than recomputed, so
        # a later threshold change cannot silently rewrite history.
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column(
            "artifact_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    )
    # The only query this table is for: "how has one arm moved over time".
    op.create_index(
        "ix_eval_run_profile_created",
        "eval_run",
        ["profile_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)
    op.drop_index("ix_eval_run_profile_created", table_name="eval_run")
    op.drop_table("eval_run")
