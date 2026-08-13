"""additive chat_session.origin so the Assistant panel's chat stops leaking into Threads

Until now /query had exactly one place to put a conversation: a chat_session
row, and every chat_session row showed up in the work rail's Threads list
(issue #208). The Research Studio's Assistant panel holds its own scratch
conversation and its turns landed in the same table through the same
ensure_session() path, so an analyst's real work got buried under assistant
chatter. ``origin`` distinguishes the two kinds at the one place they
already fork: session CREATE. The read-side filter
(``AND cs.origin = 'thread'``) lives in Go's ListChatSessionsForUser; this
migration only adds the column the filter reads.

DDL shape, in two steps for two different lock reasons:

  1. ADD COLUMN origin VARCHAR NOT NULL DEFAULT 'thread'. On Postgres 11+ a
     constant default with no volatile expression is metadata-only
     (attmissingval) -- no table rewrite, so the ACCESS EXCLUSIVE lock this
     still needs is held only long enough to update the catalog. Run under a
     bounded lock_timeout regardless (same reasoning as 0017): fail fast and
     retry rather than queue live readers/writers behind a lock this table's
     traffic cannot afford to wait for.
  2. The CHECK constraint is added NOT VALID (catalog-only, no table scan,
     brief lock) and then VALIDATEd in a separate statement (SHARE UPDATE
     EXCLUSIVE -- blocks other DDL, not reads or writes). Adding it validated
     in one step would take the stronger lock for the duration of a full
     table scan; split, only the catalog-only half needs the strong lock.

Fast-path note: attmissingval is a Postgres 11+ behavior. This migration
targets prod's actual server (Databricks Lakebase, see
docs/POLYGLOT_TARGET_2026-07-10.md) -- confirm the Lakebase Postgres version
supports it before relying on this being a non-blocking deploy; the DDL
itself is correct either way, only the lock DURATION differs.

Revision ID: 0021_chat_session_origin
Revises: 0020_eval_run
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_chat_session_origin"
down_revision: str | None = "0020_eval_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "10s"
_CHECK_NAME = "ck_chat_session_origin"


def _bounded_lock(bind: sa.engine.Connection) -> None:
    """Apply the lock timeout on Postgres; a no-op on other backends."""
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)
    op.add_column(
        "chat_session",
        sa.Column("origin", sa.String(), nullable=False, server_default="thread"),
    )
    # NOT VALID first (catalog-only); VALIDATE separately so the table-scanning
    # half runs under SHARE UPDATE EXCLUSIVE, not the stronger lock a combined
    # add-and-validate would need for the same duration.
    op.create_check_constraint(
        _CHECK_NAME,
        "chat_session",
        "origin IN ('thread', 'assistant')",
        postgresql_not_valid=True,
    )
    bind.exec_driver_sql(f"ALTER TABLE chat_session VALIDATE CONSTRAINT {_CHECK_NAME}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _bounded_lock(bind)
    op.drop_constraint(_CHECK_NAME, "chat_session", type_="check")
    op.drop_column("chat_session", "origin")
