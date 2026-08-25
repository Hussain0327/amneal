"""chunk filter indexes: btree on dosage_form, route, psg_type

``retriever._fold_filter_casing`` folds a hand-typed dosage_form / route /
psg_type filter to the corpus's stored casing by asking
``pgvector_store.distinct_metadata_values`` for that column's DISTINCT set.
None of the three columns carried an index, so each of those lookups was a full
scan of the 252 MB ``chunk`` table -- 392 ms and 105 ms server-side in the
2026-08-25 trace, re-paid every time the 60 s ``metadata_cache_ttl_s`` window
lapses. ``normalized_name`` runs the byte-identical query in 6 ms purely because
``ix_chunk_normalized_name`` exists and turns it into an index-only scan.

Plain, TRANSACTIONAL ``CREATE INDEX`` -- deliberately NOT ``CONCURRENTLY``.
Alembic runs a whole ``upgrade head`` inside ONE transaction (migrations/env.py),
so a CONCURRENTLY build's ``autocommit_block`` would commit every migration that
ran before it in that same release: a database several revisions behind could
fail here and be left committed at 0026, which the still-serving old image's
stamp guard refuses to boot against. Transactional keeps the whole release
atomic -- a failure here rolls back to the revision the run started at, releases
whatever build space it consumed, and leaves no INVALID index behind for an
operator to find. That matters most against the 512 MiB Lakebase branch cap,
where a half-finished build is what turns one failed deploy into a wedged
branch.

Two facts make the ShareLock affordable, since ``CREATE INDEX`` blocks writers
(never readers) for the build:

  * ``chunk`` is ~87.8k rows and the three columns are short text, so each build
    is seconds, not minutes.
  * The migration connection already carries ``lock_timeout`` (DB_LOCK_TIMEOUT,
    10 s by default; store/db.py:_migration_connect_args), so a build contended
    by a running Dagster ingest self-cancels with 55P03 instead of queueing --
    which is what would otherwise stall READERS behind the pending lock.
    ``regwatch release`` then exits non-zero and Fly aborts the deploy BEFORE
    replacing any long-lived machine, so prod keeps serving the old image and
    the operator retries when ingest is idle.

``ANALYZE chunk`` runs last -- legal inside a transaction (``VACUUM`` is not)
and it takes only ShareUpdateExclusiveLock, blocking neither readers nor
writers. Without it the one remaining way the planner keeps picking the seq scan
is stale post-backfill statistics.

A fresh Postgres never replays this file: its bootstrap is ``create_all`` +
``stamp head``, and the ``Chunk`` model declares the same three indexes -- as
does ``ensure_schema``'s IF-NOT-EXISTS list, the repo's third convergence route.
All three routes must produce the same index set (K4).

Revision ID: 0027_chunk_filter_indexes
Revises: 0026_ingredient_chemistry
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_chunk_filter_indexes"
down_revision: str | None = "0026_ingredient_chemistry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXED_FILTER_COLUMNS = ("dosage_form", "route", "psg_type")


def _index_name(column: str) -> str:
    """Return the index name both bootstrap routes produce for ``column``."""
    return f"ix_chunk_{column}"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for column in _INDEXED_FILTER_COLUMNS:
        # IF NOT EXISTS because create_all and ensure_schema build the same
        # three names; whichever bootstrap route ran first, the migration
        # converges rather than failing.
        op.create_index(_index_name(column), "chunk", [column], if_not_exists=True)
    op.execute(sa.text("ANALYZE chunk"))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for column in reversed(_INDEXED_FILTER_COLUMNS):
        op.drop_index(_index_name(column), table_name="chunk", if_exists=True)
