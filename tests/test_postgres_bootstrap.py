"""Postgres bootstrap integration tests (K2/K4).

These run ONLY when TEST_DATABASE_URL points at a disposable Postgres with the
pgvector extension available (the integration agent provides it), e.g.:

    docker run -d --name regwatch-mig-pg -e POSTGRES_PASSWORD=pw \
        -p 127.0.0.1:5499:5432 pgvector/pgvector:pg17
    TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:5499/postgres \
        uv run pytest tests/test_postgres_bootstrap.py

The target's public schema is DROPPED between tests — never point this at a
real database.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from types import ModuleType

import pytest
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

# A prod-shaped least-privilege role: USAGE on schema public + DML on the tables
# that already exist, NO create privilege, and it owns nothing -- so every
# runtime DDL statement comes back 42501 (insufficient_privilege). This is the
# role docs/POLYGLOT_TARGET_2026-07-10.md step 7 moves Python to. Literal SQL
# (no interpolation) keeps the role name out of any f-string.
_CREATE_LOW_PRIV_ROLE_SQL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regwatch_lowpriv_test') THEN
        CREATE ROLE regwatch_lowpriv_test LOGIN PASSWORD 'lowpriv-test-pw';
    END IF;
END $$
"""
_GRANT_LOW_PRIV_SQL = (
    "GRANT USAGE ON SCHEMA public TO regwatch_lowpriv_test",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
    "TO regwatch_lowpriv_test",
)
_DROP_LOW_PRIV_ROLE_SQL = (
    "DROP OWNED BY regwatch_lowpriv_test",
    "DROP ROLE IF EXISTS regwatch_lowpriv_test",
)


@pytest.fixture()
def pg_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Point the app at the test Postgres and wipe its public schema."""
    import config.settings as cs

    from regwatch.store import db as db_module

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # init_db asserts provider dim == vector(1536) in Postgres mode (K6), so
    # the bootstrap tests need the 1536-dim provider. No API key is required —
    # the assert reads `.dim` without instantiating a client.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()
    engine = db_module.get_engine()
    assert engine.dialect.name == "postgresql", "TEST_DATABASE_URL must be a postgres URL"
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield db_module
    db_module.reset_for_tests()


@pytest.fixture()
def low_priv_engine(pg_db: ModuleType) -> Iterator[Callable[..., Engine]]:
    """Factory for engines connected as the DML-only role (see the SQL above).

    Grants are applied on every call because ``GRANT ... ON ALL TABLES`` only
    covers tables that already exist -- callers create the schema as the
    superuser first, then ask for a low-privilege engine.
    """
    admin = pg_db.get_engine()
    with admin.begin() as conn:
        conn.execute(text(_CREATE_LOW_PRIV_ROLE_SQL))
    built: list[Engine] = []

    def _make(*, read_only: bool = False) -> Engine:
        with admin.begin() as conn:
            for statement in _GRANT_LOW_PRIV_SQL:
                conn.execute(text(statement))
        # Derive from the app engine's URL so the psycopg-v3 driver form that
        # config.settings normalizes to is carried over verbatim.
        url = admin.url.set(username="regwatch_lowpriv_test", password="lowpriv-test-pw")
        # read_only models the same role on a replica / with the read-only GUC
        # set (SQLSTATE 25006 instead of 42501) -- POLYGLOT step 7's end state.
        connect_args = {"options": "-c default_transaction_read_only=on"} if read_only else {}
        engine = create_engine(url, connect_args=connect_args)
        built.append(engine)
        return engine

    yield _make
    for engine in built:
        engine.dispose()
    with admin.begin() as conn:
        for statement in _DROP_LOW_PRIV_ROLE_SQL:
            conn.execute(text(statement))


def _stamped_revision(db_module: ModuleType) -> str | None:
    with db_module.get_engine().connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return row


def test_fresh_bootstrap_creates_schema_and_stamps_head(pg_db: ModuleType) -> None:
    pg_db.init_db()
    engine = pg_db.get_engine()
    tables = set(inspect(engine).get_table_names())
    expected = {
        "product",
        "psg_document",
        "psg_version",
        "be_requirement",
        "query_log",
        "chat_session",
        "chat_message",
        "user",
        "auth_session",
        "ob_product",
        "ob_patent",
        "ob_exclusivity",
        "spl_document",
        "chunk",
        "alert",
        "alembic_version",
    }
    assert expected <= tables
    head = pg_db._head_revision(pg_db._alembic_config())
    assert _stamped_revision(pg_db) == head


def test_bootstrap_enables_rls_on_every_public_table(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
        ).all()
        policies = conn.execute(
            text("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        ).scalar()
    assert rows, "expected public tables after bootstrap"
    without_rls = sorted(name for name, enabled in rows if not enabled)
    assert not without_rls, f"tables missing RLS: {without_rls}"
    # Deny-all: RLS enabled with NO policies (Data-API roles get nothing).
    assert policies == 0


def test_fresh_bootstrap_installs_ensure_rls_event_trigger(pg_db: ModuleType) -> None:
    """The fresh-boot path (create_all + stamp head) NEVER replays migration 0011,
    so it must install 0011's `ensure_rls` event trigger itself -- otherwise a
    freshly-bootstrapped DB silently diverges from a migrate-replayed one and
    leaves tables created AFTER boot un-RLSed (auto-exposed by the Supabase Data
    API). Regression guard for the 0011 empty-boot convergence gap."""
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        triggers = {row[0] for row in conn.execute(text("SELECT evtname FROM pg_event_trigger"))}
    assert "ensure_rls" in triggers, "fresh bootstrap did not install the ensure_rls trigger"


def test_fresh_bootstrap_trigger_rls_es_table_created_after_boot(pg_db: ModuleType) -> None:
    """End-to-end on the bootstrap path: a table created AFTER init_db must come
    up RLS-enabled with no explicit ALTER, proving the installed trigger fires
    (not just that the catalog row exists)."""
    pg_db.init_db()
    engine = pg_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE public.post_boot_probe (id int)"))
    with engine.connect() as conn:
        relrowsecurity = conn.execute(
            text(
                "SELECT c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname='public' AND c.relname='post_boot_probe'"
            )
        ).scalar()
    assert relrowsecurity is True, "post-boot table was not auto-RLSed by ensure_rls"


def test_rls_enable_skips_already_protected_table_under_read_lock(pg_db: ModuleType) -> None:
    """Steady-state boot must take NO ACCESS EXCLUSIVE lock on a table that
    already has RLS — the 2026-06-18 incident was re-ALTERing the already-
    protected `chunk` table on every boot, blocking behind a live reader.

    With a reader holding an ACCESS SHARE lock on `chunk`, a (wrongly) re-issued
    ALTER would block until the 3s lock_timeout; skipping already-RLS tables
    returns near-instantly instead.
    """
    pg_db.init_db()
    engine = pg_db.get_engine()
    holder = engine.connect()
    trans = holder.begin()
    holder.execute(text("SELECT * FROM chunk"))  # ACCESS SHARE lock, held open
    try:
        start = time.monotonic()
        pg_db._enable_row_level_security(engine)  # must not touch `chunk`
        elapsed = time.monotonic() - start
    finally:
        trans.rollback()
        holder.close()
    assert elapsed < 2.0, "boot RLS-enable blocked on an already-protected table"


def test_rls_enable_degrades_gracefully_on_contended_lock(pg_db: ModuleType) -> None:
    """A genuinely-pending table whose lock is held during boot must NOT crash
    the process: the ALTER hits lock_timeout, is logged and skipped, and the
    boot proceeds (an already-running instance keeps serving; a later boot
    re-attempts once the lock frees)."""
    pg_db.init_db()
    engine = pg_db.get_engine()
    # init_db() now installs 0011's `ensure_rls` event trigger, which auto-RLSes
    # any newly CREATEd public table -- so a fresh CREATE no longer lands in the
    # RLS-OFF "pending" set on its own. DISABLE it right back OFF to recreate the
    # genuinely-pending state this test needs (ALTER ... DISABLE is not one of the
    # trigger's tags -- CREATE TABLE / CREATE TABLE AS / SELECT INTO -- so it does
    # not re-fire). This keeps the real lock-timeout degradation path exercised.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE lock_probe (id int)"))
        conn.execute(text("ALTER TABLE lock_probe DISABLE ROW LEVEL SECURITY"))

    holder = engine.connect()
    trans = holder.begin()
    holder.execute(text("SELECT * FROM lock_probe"))  # blocks the ALTER's lock
    try:
        # Returns (swallows the lock_timeout) rather than raising -> boot lives.
        pg_db._enable_row_level_security(engine)
    finally:
        trans.rollback()
        holder.close()

    with engine.connect() as conn:
        probe_rls = conn.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = 'lock_probe'")
        ).scalar()
    # The contended table was skipped (still no RLS) and nothing crashed.
    assert probe_rls is False


def test_ready_fails_closed_when_rls_sweep_left_a_table_unprotected(
    pg_db: ModuleType,
) -> None:
    """Boot tolerates a skipped ALTER (the test above); READINESS must not.

    Same contended-lock construction: the table stays un-RLSed, which on
    Supabase means an anon-key holder can read it over the Data API. The
    machine must therefore report not_ready and stay out of rotation until a
    later boot's ALTER lands. Before this gate the skip was invisible to
    everything except a manual Supabase Advisors check.
    """
    from fastapi import Response

    from regwatch.api import main

    pg_db.init_db()
    engine = pg_db.get_engine()
    # CREATE then DISABLE for the same reason as the test above: 0011's
    # `ensure_rls` event trigger auto-RLSes new tables, and ALTER ... DISABLE is
    # not one of its tags, so it does not re-fire.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE ready_probe (id int)"))
        conn.execute(text("ALTER TABLE ready_probe DISABLE ROW LEVEL SECURITY"))

    holder = engine.connect()
    trans = holder.begin()
    holder.execute(text("SELECT * FROM ready_probe"))  # blocks the ALTER's lock
    try:
        pg_db._enable_row_level_security(engine)  # skips + records, never raises
    finally:
        trans.rollback()
        holder.close()

    assert "ready_probe" in pg_db.unprotected_public_tables()
    response = Response()
    body = main.ready(response)
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["rls"] is False
    assert body["failed"] == "rls"
    # The anonymous body must never name the unprotected table -- that would
    # hand an anon-key holder the target. Names go to the log + Sentry.
    assert "ready_probe" not in body["detail"]

    # A later, uncontended sweep clears it: the machine returns to rotation and
    # the healthy body keeps its original three-key shape (no `rls` key).
    pg_db._enable_row_level_security(engine)
    assert pg_db.unprotected_public_tables() == ()
    response = Response()
    body = main.ready(response)
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["checks"] == {"db": True, "vector_store": True, "llm": True}


def test_ensure_postgres_objects_degrades_gracefully_on_contended_lock(
    pg_db: ModuleType,
) -> None:
    """The boot-time chunk DDL must NOT crash the process when `chunk` is locked
    by a concurrent writer. CREATE INDEX IF NOT EXISTS takes a ShareLock on chunk
    even when the index already exists — the 2026-06-18 lock-pileup mechanism on
    a sibling code path to RLS. Under a held ROW EXCLUSIVE lock the boot DDL must
    hit lock_timeout, log, and skip — not raise — so an in-flight ingest can't
    crash-loop a booting machine."""
    pg_db.init_db()
    engine = pg_db.get_engine()

    holder = engine.connect()
    trans = holder.begin()
    # ROW EXCLUSIVE (what an INSERT/UPSERT writer holds) conflicts with the
    # ShareLock CREATE INDEX needs -> the boot DDL must time out, not hang/raise.
    holder.execute(text("LOCK TABLE chunk IN ROW EXCLUSIVE MODE"))
    try:
        start = time.monotonic()
        pg_db._ensure_postgres_objects(engine)  # must return, not raise
        elapsed = time.monotonic() - start
    finally:
        trans.rollback()
        holder.close()
    # Bounded: one 3s lock_timeout then break, not 3s per every index.
    assert elapsed < 6.0, "boot chunk-DDL did not bound its lock wait"


def test_ensure_schema_degrades_gracefully_on_contended_lock(pg_db: ModuleType) -> None:
    """pgvector_store.ensure_schema is the lazy first-use twin of the boot-time
    chunk DDL (it re-runs the same CREATE INDEX IF NOT EXISTS statements on the
    first similarity_search/add_chunks of a process), so it must degrade the same
    way: under a held ROW EXCLUSIVE lock from a concurrent ingest writer the DDL
    hits lock_timeout, logs, and skips -- not hang or surface as an uncaught 500
    on the first request."""
    from regwatch.store import pgvector_store

    pg_db.init_db()
    engine = pg_db.get_engine()

    holder = engine.connect()
    trans = holder.begin()
    # ROW EXCLUSIVE (what an INSERT/UPSERT writer holds) conflicts with the
    # ShareLock CREATE INDEX needs -> the first-use DDL must time out, not raise.
    holder.execute(text("LOCK TABLE chunk IN ROW EXCLUSIVE MODE"))
    try:
        start = time.monotonic()
        pgvector_store.ensure_schema(engine)  # must return, not raise
        elapsed = time.monotonic() - start
    finally:
        trans.rollback()
        holder.close()
    # Bounded: one 3s lock_timeout then break, not 3s per every index.
    assert elapsed < 6.0, "first-use chunk DDL did not bound its lock wait"


def test_boot_ddl_degrades_on_insufficient_privilege(
    pg_db: ModuleType, low_priv_engine: Callable[..., Engine]
) -> None:
    """A role with DML but no DDL privilege must NOT crash the process.

    Every runtime-DDL site guarded only lock contention (OperationalError /
    55P03). A least-privilege role is refused with 42501, which arrives as a
    DIFFERENT exception class and escaped every guard. Boot funnels through
    init_db (api/main.py lifespan, the CLI entry points, ingest/pipeline.py), so
    that crash-looped the machine; pgvector_store.ensure_schema additionally
    runs LAZILY on a booted process's first query, where the same escape is a
    naked, unaudited 500 (INV-6).
    """
    from regwatch.store import pgvector_store

    pg_db.init_db()
    admin = pg_db.get_engine()
    low = low_priv_engine()
    # Push each site past its "already converged" early return so the DDL is
    # genuinely attempted (and genuinely denied) instead of skipped.
    with admin.begin() as conn:
        conn.execute(text("DROP TRIGGER embedding_profile_immutable ON embedding_profile"))
        conn.execute(text("DROP EVENT TRIGGER ensure_rls"))
        conn.execute(text("ALTER TABLE chunk DISABLE ROW LEVEL SECURITY"))
    try:
        pg_db._ensure_postgres_objects(low)  # chunk DDL loop + 0015 trigger DDL
        pg_db._ensure_rls_event_trigger(low)  # no try/except at all today
        pgvector_store.ensure_schema(low)  # lazy first-query twin + chunk RLS
        with admin.connect() as conn:
            profile_trigger = conn.execute(
                text(
                    "SELECT 1 FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname = 'embedding_profile_immutable'"
                )
            ).scalar()
            event_triggers = {
                row[0] for row in conn.execute(text("SELECT evtname FROM pg_event_trigger"))
            }
            chunk_rls = conn.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = 'chunk'")
            ).scalar()
        # Non-vacuous: the statements really were refused. If the role ever
        # gained DDL rights these would flip and the test would stop proving
        # anything, so assert the denial, not just the absence of a raise.
        assert profile_trigger is None
        assert "ensure_rls" not in event_triggers
        assert chunk_rls is False
    finally:
        # The per-test reset only TRUNCATEs, so hand the schema back intact.
        with admin.begin() as conn:
            conn.execute(text("ALTER TABLE chunk ENABLE ROW LEVEL SECURITY"))
        pg_db._ensure_postgres_objects(admin)
        pg_db._ensure_rls_event_trigger(admin)


def test_boot_ddl_degrades_on_read_only_transaction(
    pg_db: ModuleType, low_priv_engine: Callable[..., Engine]
) -> None:
    """Same sites, SQLSTATE 25006: a read-only role/replica must not crash boot.

    docs/POLYGLOT_TARGET_2026-07-10.md:158 makes a read-only Python role the
    target end state, and a hot standby answers every DDL the same way today.
    _ensure_vector_extension is the first statement boot runs and has no
    degrade guard by design (a genuinely absent extension must stay a hard
    refusal), so it has to STOP ASKING when the extension is already installed.
    """
    from regwatch.store import pgvector_store

    pg_db.init_db()
    low_ro = low_priv_engine(read_only=True)
    pg_db._ensure_vector_extension(low_ro)
    pg_db._ensure_postgres_objects(low_ro)
    pg_db._ensure_rls_event_trigger(low_ro)
    pgvector_store.ensure_schema(low_ro)


def test_rls_sweep_still_fails_closed_on_insufficient_privilege(
    pg_db: ModuleType, low_priv_engine: Callable[..., Engine]
) -> None:
    """The compliance case: a denied ALTER must not blind the /ready RLS gate.

    Boot deliberately TOLERATES an ALTER it could not run, and the only thing
    that keeps that from being silent is _record_unprotected_tables running
    afterwards. A 42501 propagating out of the ALTER skipped it entirely, so
    unprotected_public_tables() stayed empty and /ready answered "ready" over a
    public table any anon-key holder can read through the Supabase Data API.
    """
    from fastapi import Response

    from regwatch.api import main

    pg_db.init_db()
    admin = pg_db.get_engine()
    low = low_priv_engine()
    with admin.begin() as conn:
        conn.execute(text("ALTER TABLE query_log DISABLE ROW LEVEL SECURITY"))
    try:
        pg_db._enable_row_level_security(low)  # denied ALTER must not escape ...
        with admin.connect() as conn:
            query_log_rls = conn.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = 'query_log'")
            ).scalar()
        assert query_log_rls is False  # ... the table IS still unprotected ...
        assert "query_log" in pg_db.unprotected_public_tables()  # ... and it is published
        response = Response()
        body = main.ready(response)
        assert response.status_code == 503
        assert body["status"] == "not_ready"
        assert body["failed"] == "rls"
    finally:
        with admin.begin() as conn:
            conn.execute(text("ALTER TABLE query_log ENABLE ROW LEVEL SECURITY"))
        pg_db._enable_row_level_security(admin)  # clears the recorded set


def test_non_degradable_ddl_error_still_raises(
    pg_db: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control on the widened guard: only ENVIRONMENTAL refusals degrade.

    A SQLSTATE outside the allowlist means our own DDL is wrong, and booting
    into a silently half-built schema is worse than refusing to boot.
    """
    pg_db.init_db()
    monkeypatch.setattr(
        pg_db,
        "_CHUNK_INDEX_DDL",
        ("CREATE INDEX IF NOT EXISTS ix_chunk_bogus ON chunk (no_such_column)",),
    )
    with pytest.raises(ProgrammingError):
        pg_db._ensure_postgres_objects(pg_db.get_engine())


def test_ensure_vector_extension_skips_create_when_already_installed(pg_db: ModuleType) -> None:
    """No CREATE EXTENSION once pgvector is installed.

    Postgres checks read-only/privilege BEFORE the IF NOT EXISTS existence
    check, so the un-probed statement fails on a read-only role even though
    there is nothing to do (verified: 25006 in a read-only transaction).
    """
    pg_db.init_db()
    engine = pg_db.get_engine()
    seen: list[str] = []

    def _record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        pg_db._ensure_vector_extension(engine)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert [s for s in seen if "CREATE EXTENSION" in s.upper()] == []


def test_ddl_degrade_reason_classifies_only_environmental_sqlstates() -> None:
    """Unit guard on the allowlist itself (no DB round trip)."""
    from regwatch.store.db import ddl_degrade_reason

    class _PgError(Exception):
        def __init__(self, sqlstate: str | None) -> None:
            super().__init__("boom")
            self.sqlstate = sqlstate

    def _wrap(sqlstate: str | None) -> DBAPIError:
        return DBAPIError("SELECT 1", {}, _PgError(sqlstate))

    assert ddl_degrade_reason(_wrap("55P03")) == "lock_not_available"
    assert ddl_degrade_reason(_wrap("42501")) == "insufficient_privilege"
    assert ddl_degrade_reason(_wrap("25006")) == "read_only_transaction"
    # 57014 must stay degradable: psycopg maps it to OperationalError, so the
    # guards this allowlist replaced already swallowed it. Dropping it would
    # crash-loop boot when a re-attempted HNSW build hits statement_timeout.
    assert ddl_degrade_reason(_wrap("57014")) == "query_canceled"
    # Our DDL being wrong (syntax error, undefined table/column) is a bug.
    assert ddl_degrade_reason(_wrap("42601")) is None
    assert ddl_degrade_reason(_wrap("42P01")) is None
    # A dead connection carries no SQLSTATE -- not something boot may skip past.
    assert ddl_degrade_reason(_wrap(None)) is None


def test_bootstrap_creates_chunk_table_and_indexes(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        embedding_type = conn.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = 'public.chunk'::regclass AND a.attname = 'embedding'"
            )
        ).scalar()
        index_defs = {
            name: ddl
            for name, ddl in conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunk'")
            )
        }
    assert embedding_type == "vector(1536)"
    assert "hnsw" in index_defs["ix_chunk_embedding_hnsw"]
    # Must match the btree indexes the pgvector_store.Chunk model declares —
    # both bootstrap paths produce the same schema regardless of import order.
    assert {
        "ix_chunk_normalized_name",
        "ix_chunk_doc_id",
        "ix_chunk_version_id",
        "ix_chunk_appl_no",
    } <= set(index_defs)


def test_bootstrap_creates_chat_session_composite_index(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'chat_session'")
            )
        }
    assert "ix_chat_session_user_id_updated_at" in names


def test_json_columns_are_jsonb_on_postgres(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().connect() as conn:
        data_type = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'query_log' "
                "AND column_name = 'route_json'"
            )
        ).scalar()
    assert data_type == "jsonb"


def test_round_trip_through_session_scope(pg_db: ModuleType) -> None:
    from sqlmodel import select

    from regwatch.store.models import Product

    pg_db.init_db()
    with pg_db.session_scope() as s:
        s.add(
            Product(
                active_ingredient="Albuterol Sulfate",
                normalized_name="albuterol",
                source="manual",
            )
        )
    with pg_db.session_scope() as s:
        rows = list(s.scalars(select(Product).where(Product.normalized_name == "albuterol")))
    assert len(rows) == 1


def test_second_boot_is_idempotent(pg_db: ModuleType) -> None:
    pg_db.init_db()
    pg_db.init_db()  # stamped at head -> verify + idempotent ensures, no error
    head = pg_db._head_revision(pg_db._alembic_config())
    assert _stamped_revision(pg_db) == head


def test_refuses_to_start_on_revision_mismatch(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '0000_bogus'"))
    # init_db is memoized per process; reset to simulate a fresh process boot
    # against the tampered DB — the state the boot refusal actually guards.
    pg_db.reset_for_tests()
    with pytest.raises(RuntimeError, match="stamped at alembic revision"):
        pg_db.init_db()


def test_alembic_upgrade_head_heals_0007_stamped_postgres(pg_db: ModuleType) -> None:
    """H3 existing-Postgres path: a 0007-stamped DB reaches head via the
    documented operator one-liner (`alembic upgrade head`, DEPLOY.md §3/§6.3)
    after this build's boot refusal — 0008's batch ops are PG-compatible."""
    from alembic import command

    pg_db.init_db()  # fresh bootstrap: create_all + stamp head
    cfg = pg_db._alembic_config()
    # Rewind to the 0007 shape (0008's downgrade runs fine on Postgres),
    # leaving the stamp at 0007 — the state a parallel cutover produces.
    command.downgrade(cfg, "0007_chat_session_user_updated")
    inspector = inspect(pg_db.get_engine())
    assert "answer_feedback" not in inspector.get_table_names()
    assert "input_tokens" not in {c["name"] for c in inspector.get_columns("query_log")}
    assert _stamped_revision(pg_db) == "0007_chat_session_user_updated"

    # Booting this build against it refuses (by design) ... reset the per-
    # process init_db memoization first so this models an actual fresh boot.
    pg_db.reset_for_tests()
    with pytest.raises(RuntimeError, match="stamped at alembic revision"):
        pg_db.init_db()

    # ... and the documented one-liner heals it.
    command.upgrade(cfg, "head")
    pg_db.reset_for_tests()
    inspector = inspect(pg_db.get_engine())
    cols = {c["name"] for c in inspector.get_columns("query_log")}
    assert {"input_tokens", "output_tokens", "cost_usd"} <= cols
    assert "answer_feedback" in inspector.get_table_names()
    pg_db.init_db()  # boots clean at head
    head = pg_db._head_revision(pg_db._alembic_config())
    assert _stamped_revision(pg_db) == head


def test_refuses_to_start_on_unstamped_nonempty_database(pg_db: ModuleType) -> None:
    pg_db.init_db()
    with pg_db.get_engine().begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    # init_db is memoized per process; reset to simulate a fresh process boot
    # against the now-unstamped DB — the state the boot refusal actually guards.
    pg_db.reset_for_tests()
    with pytest.raises(RuntimeError, match="no alembic_version stamp"):
        pg_db.init_db()


def test_init_db_fails_fast_on_embedding_dim_mismatch(
    pg_db: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K6: a wrong-dim provider must refuse at startup, not 500 at first use."""
    import config.settings as cs

    monkeypatch.setenv("EMBEDDING_PROVIDER", "local-bge-small")  # 384-dim
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    with pytest.raises(RuntimeError, match="384-dim"):
        pg_db.init_db()
