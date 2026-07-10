"""unique (psg_document_id, content_hash) on psg_version

Closes the duplicate-revision race for good: two overlapping ingest runs (the
watch-daily cron plus a manual ``ingest-all``) could both pass the pipeline's
latest-hash check and insert the same revision twice, and the duplicate
never-alerted row would re-alert the same FDA change on the next run (INV-4).
With the unique index the loser's INSERT fails and the pipeline handles it as
the existing skip path.

Dedupe-first: prod data may already carry duplicate (psg_document_id,
content_hash) rows from the pre-constraint era, so the index build would fail
on them. A group's rows carry identical content (same document, same content
hash, same parsed-text path), so the dedupe keeps the row the pipeline already
treats as latest (max ``captured_at``, id as tiebreak) and, per deleted loser:
  * repoints ``be_requirement.version_id`` to the keeper (readers pick the
    max-(version_id, id) row per doc, so an extra row per version is inert);
  * repoints ``alert.psg_version_id`` to the keeper where that does not
    collide with ``uq_alert_version_listing_product``, and deletes the
    remainder. For a RACE-produced group the colliding loser alert is the
    same FDA change already alerted against the keeper -- exactly the INV-4
    duplicate this index prevents. But a pre-0014 group can also be a
    legitimate revert the old pipeline recorded (content A -> B -> back to
    A): there the colliding alerts mark DISTINCT FDA events, so every alert
    this migration deletes is printed first as an operator-reviewable
    manifest in the release output (deletes are not restored by
    ``downgrade``). Keeping every alert pointed at a real psg_version
    preserves INV-4's referential promise;
  * repoints ``chunk.version_id`` (Postgres only, when the pgvector table
    exists) so the live index does not trigger a needless chunk backfill.

The loser psg_version rows themselves are then deleted. That data change is
NOT restored by ``downgrade`` (which only drops the index) -- the deleted rows
are redundant duplicates whose content the keeper still carries, so no
information is lost. Old application builds keep working against the new
index: their un-caught duplicate INSERT surfaces as one logged ingest error
for that listing instead of a silent duplicate row (backward compatible, no
crash loop).

Plain ``CREATE UNIQUE INDEX`` (not CONCURRENTLY): migrations/env.py runs each
migration inside a transaction, where CONCURRENTLY is illegal, and psg_version
is a few-thousand-row table -- the SHARE lock is held for well under a second
and the migration connection's ``lock_timeout`` (env.py) bounds any wait. A
concurrent writer landing a NEW duplicate between the dedupe and the index
build makes the migration fail loudly and roll back -- the safe outcome; the
watch-daily concurrency group makes that window academic. Dialect-portable:
the same dedupe SQL and index apply to the live Supabase Postgres via
``alembic upgrade head`` AND to SQLite via ``_init_sqlite``'s
``command.upgrade``. A FRESH Postgres never replays this file -- its bootstrap
is ``create_all`` + ``stamp head``, and ``models.py``'s ``PsgVersion`` declares
the same index, so both paths produce the same schema.

Revision ID: 0014_psg_version_unique_hash
Revises: 0013_whitepaper_runs
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_psg_version_unique_hash"
down_revision: str | None = "0013_whitepaper_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Losers = every row of a (psg_document_id, content_hash) group except the one
# the pipeline already serves as latest (max captured_at, max id as tiebreak).
# The window frame default (partition start .. current row) makes FIRST_VALUE
# the keeper for every rn > 1 row on both Postgres and SQLite.
_DUPLICATE_LOSERS_SQL = """
SELECT id, keeper_id FROM (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY psg_document_id, content_hash
               ORDER BY captured_at DESC, id DESC
           ) AS rn,
           FIRST_VALUE(id) OVER (
               PARTITION BY psg_document_id, content_hash
               ORDER BY captured_at DESC, id DESC
           ) AS keeper_id
    FROM psg_version
) ranked
WHERE rn > 1
"""

# Repoint a loser's alerts unless the keeper already carries an alert for the
# same (listing_appl_no, product_id) -- that pair IS the alert-level duplicate.
_REPOINT_ALERTS_SQL = """
UPDATE alert SET psg_version_id = :keeper
WHERE psg_version_id = :loser
  AND NOT EXISTS (
      SELECT 1 FROM alert a2
      WHERE a2.psg_version_id = :keeper
        AND a2.listing_appl_no = alert.listing_appl_no
        AND a2.product_id = alert.product_id
  )
"""


def upgrade() -> None:
    bind = op.get_bind()
    losers = bind.execute(sa.text(_DUPLICATE_LOSERS_SQL)).all()
    # The pgvector chunk table lives outside alembic's metadata and only exists
    # in Postgres mode (and only once the vector bootstrap has run).
    has_chunk_table = bind.dialect.name == "postgresql" and sa.inspect(bind).has_table("chunk")
    # Loser-by-loser (not one bulk UPDATE): two losers repointing alerts for the
    # same (listing, product) onto one keeper must see each other, so the second
    # falls through to the duplicate-delete instead of violating the alert
    # unique key mid-statement. Duplicate groups are rare (race-era only), so
    # the loop is a handful of statements at most.
    for loser_id, keeper_id in losers:
        params = {"loser": loser_id, "keeper": keeper_id}
        bind.execute(
            sa.text("UPDATE be_requirement SET version_id = :keeper WHERE version_id = :loser"),
            params,
        )
        bind.execute(sa.text(_REPOINT_ALERTS_SQL), params)
        # Alerts still on the loser after the repoint are about to be deleted
        # for good (downgrade does not restore data). A revert-shaped group's
        # colliding alerts are real, distinct FDA events, so print each doomed
        # row as an operator-reviewable manifest in the release output.
        doomed_alerts = bind.execute(
            sa.text(
                "SELECT id, listing_appl_no, product_id, captured_at "
                "FROM alert WHERE psg_version_id = :loser"
            ),
            params,
        ).all()
        for alert_row in doomed_alerts:
            print(
                "0014: deleting alert "
                f"id={alert_row.id} listing_appl_no={alert_row.listing_appl_no} "
                f"product_id={alert_row.product_id} captured_at={alert_row.captured_at} "
                f"(psg_version {loser_id}; keeper {keeper_id} already carries this pair)"
            )
        bind.execute(sa.text("DELETE FROM alert WHERE psg_version_id = :loser"), params)
        if has_chunk_table:
            bind.execute(
                sa.text("UPDATE chunk SET version_id = :keeper WHERE version_id = :loser"),
                params,
            )
        bind.execute(sa.text("DELETE FROM psg_version WHERE id = :loser"), params)
    op.create_index(
        "uq_psg_version_doc_hash",
        "psg_version",
        ["psg_document_id", "content_hash"],
        unique=True,
    )


def downgrade() -> None:
    # Schema-reversible only: the dedupe deletes above are redundant duplicates
    # and are intentionally not resurrected.
    op.drop_index("uq_psg_version_doc_hash", table_name="psg_version")
