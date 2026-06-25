"""ensure_rls event trigger: auto-enable deny-all RLS on NEW public tables

``store/db.py::_enable_row_level_security`` enables deny-all RLS only on the
tables that exist at boot. Any table created AFTER boot -- a future migration, a
manual ``CREATE TABLE``, or a Supabase Studio table -- is left WITHOUT RLS and is
therefore auto-exposed by Supabase's Data API (PostgREST) to the anon/
authenticated roles (RLS-without-policies is the deny-all we rely on). This adds
a Postgres event trigger that fires on every table-creating DDL in ``public``
(``CREATE TABLE``, ``CREATE TABLE AS``, ``SELECT INTO``) and enables RLS on the
new table, closing that gap at the source.

WHY the dialect guard: event triggers + ``CREATE EVENT TRIGGER`` are
Postgres-only DDL; SQLite has no such concept, so on SQLite this migration is an
intentional no-op (the SQLite path has no Data API and no anon role to protect).
The portable nature of the chain is preserved: ``_init_sqlite``'s
``command.upgrade`` replays this file and simply skips the body.

Lock-safe: creating a function and an event trigger takes NO table locks, so
this never blocks live read traffic (unlike the 2026-06-18 ``ALTER TABLE`` lock
pileup). The trigger body's per-new-table ``ALTER TABLE ... ENABLE ROW LEVEL
SECURITY`` runs inside the SAME transaction that just created the table, so it
only ever touches a brand-new, uncontended relation. Fully reversible: the
downgrade DROPs both the trigger and the function.

A FRESH Postgres never replays this file -- its bootstrap is ``create_all`` +
``stamp head`` -- so ``scripts/migrate_to_supabase.py`` re-runs the same
idempotent DDL after bootstrap (it imports ``rls_event_trigger_sql`` from here)
to guarantee the trigger exists on a freshly-migrated database too.

Revision ID: 0011_ensure_rls_event_trigger
Revises: 0010_chat_message_provenance
Create Date: 2026-06-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_ensure_rls_event_trigger"
down_revision: str | None = "0010_chat_message_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The function + event trigger are CREATE-OR-REPLACE / DROP-IF-EXISTS-then-CREATE
# so this DDL is idempotent: it is safe to run from the migration AND, on a fresh
# Postgres that skipped migration replay, from scripts/migrate_to_supabase.py.
# pg_event_trigger_ddl_commands() yields one row per object touched by the firing
# CREATE; we enable RLS only on ordinary tables ('table') in the public schema.
# SECURITY DEFINER so the trigger can ALTER a table created by a lower-priv role;
# search_path is pinned empty to keep the definer context from being hijacked.
_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION public.rls_auto_enable()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    obj record;
BEGIN
    FOR obj IN SELECT * FROM pg_event_trigger_ddl_commands()
    LOOP
        IF obj.object_type = 'table'
           AND obj.schema_name = 'public'
           AND obj.objid IS NOT NULL
        THEN
            EXECUTE format(
                'ALTER TABLE %s ENABLE ROW LEVEL SECURITY',
                obj.object_identity
            );
        END IF;
    END LOOP;
END;
$$;
"""

_TRIGGER_DROP = "DROP EVENT TRIGGER IF EXISTS ensure_rls"
# Cover every command tag that creates a table: plain CREATE TABLE (incl.
# partitions), CREATE TABLE AS, and the legacy SELECT INTO. All three emit an
# object_type='table' row from pg_event_trigger_ddl_commands(), so the function
# body RLSes them uniformly. Listing CTAS/SELECT INTO is defense-in-depth for
# manual / Supabase-Studio DDL (the app itself emits only plain CREATE TABLE);
# without them such a table would stay un-RLSed until the next boot sweep.
_TRIGGER_CREATE = (
    "CREATE EVENT TRIGGER ensure_rls ON ddl_command_end "
    "WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO') "
    "EXECUTE FUNCTION public.rls_auto_enable()"
)


def rls_event_trigger_sql() -> tuple[str, ...]:
    """Idempotent statements that (re)create rls_auto_enable() + ensure_rls.

    Importable so the fresh-Postgres bootstrap (create_all + stamp head, which
    never replays migrations) can apply the SAME trigger via
    scripts/migrate_to_supabase.py. Postgres-only; the caller dialect-guards.
    """
    return (_FUNCTION_DDL, _TRIGGER_DROP, _TRIGGER_CREATE)


def upgrade() -> None:
    # Postgres-only: SQLite has no event triggers (and no Data API to protect).
    if op.get_bind().dialect.name != "postgresql":
        return
    for stmt in rls_event_trigger_sql():
        op.execute(stmt)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP EVENT TRIGGER IF EXISTS ensure_rls")
    op.execute("DROP FUNCTION IF EXISTS public.rls_auto_enable()")
