"""Database session management — Postgres via DATABASE_URL (the only mode).

Postgres-only since R5 (docs/POLYGLOT_TARGET_2026-07-10.md): DATABASE_URL is
mandatory and ``get_engine`` refuses to build anything else — the SQLite
fallback that once served dev/tests is gone (tests run against a disposable
Postgres named by TEST_DATABASE_URL). ``init_db`` runs the fresh-Postgres
bootstrap: ``create_all`` + ``alembic stamp head`` — NO history replay,
because migrations 0001-0006 contain SQLite-specific batch ops from the
pre-Supabase era.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from config.settings import Settings, get_settings
from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine

from regwatch.common.logging import get_logger

log = get_logger(__name__)

_engine: Engine | None = None
_engine_lock = Lock()
# init_db is memoized per process: idempotent but not free (inspect + DDL + RLS
# round-trips), and ingest calls it once per listing (~1,795x in a full crawl).
# A SEPARATE lock from _engine_lock — init_db calls get_engine(), which takes
# _engine_lock, so reusing it here would deadlock (Lock is not reentrant).
_init_lock = Lock()
_initialized = False

# K4: the pgvector chunk store. The embedding column is raw DDL (the `vector`
# type comes from the pgvector extension); everything else mirrors the Chroma
# metadata fields so the vector-store interface can dispatch transparently.
# {vector_schema} is the schema the extension actually lives in — `extensions`
# on Supabase, `public` on a local docker Postgres. Qualifying the type and
# the opclass means the DDL works even when that schema isn't on the
# connection's search_path (Supabase's default search_path does include
# `extensions`, but we don't depend on it).
_CHUNK_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS chunk (
    id TEXT PRIMARY KEY,
    doc_id INTEGER,
    version_id INTEGER,
    page INTEGER,
    section_path TEXT,
    normalized_name TEXT,
    dosage_form TEXT,
    route TEXT,
    source_url TEXT,
    psg_type TEXT,
    appl_no TEXT,
    short_name TEXT,
    text TEXT,
    embedding {vector_schema}.vector(1536)
)
"""
_CHUNK_INDEX_DDL = (
    # HNSW for the unfiltered scan path (cosine); filtered queries use the
    # btree columns + exact ORDER BY over the narrowed set (K5). This set must
    # stay identical to what `pgvector_store.Chunk` declares (index=True
    # columns + the HNSW DDL in ensure_schema) — both bootstrap paths must
    # produce the same schema regardless of module-initialization order.
    "CREATE INDEX IF NOT EXISTS ix_chunk_embedding_hnsw ON chunk "
    "USING hnsw (embedding {vector_schema}.vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_normalized_name ON chunk (normalized_name)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_doc_id ON chunk (doc_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_version_id ON chunk (version_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_appl_no ON chunk (appl_no)",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _active_database_url() -> str:
    return _required_database_url()


def _required_database_url() -> str:
    """DATABASE_URL, or a loud refusal — there is no fallback datastore.

    Preserves the B1 fail-loud posture unconditionally: booting without a
    configured Postgres once meant silently landing on the container's
    ephemeral SQLite disk; now it is simply an error at first engine use.
    """
    url = (get_settings().database_url or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is empty — Postgres is the only datastore since R5 "
            "(the SQLite fallback is gone). Set DATABASE_URL to a "
            "Postgres/Supabase URL; tests use TEST_DATABASE_URL (see "
            "tests/conftest.py)."
        )
    return url


# Hosts reached over a trusted/loopback path where TLS is neither configured
# nor needed: CI's Postgres service container (localhost:5432, see
# .github/workflows/ci.yml) and the local docker-compose Postgres. Forcing
# sslmode=require on these would break the DB integration tests and local dev,
# since neither speaks SSL.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def _enforce_sslmode(database_url: str) -> URL:
    """Force TLS for remote Postgres; leave local/CI Postgres untouched.

    The Supabase session pooler is reached over the PUBLIC internet
    (aws-1-us-east-1.pooler.supabase.com), so the connection MUST be encrypted.
    We add the libpq ``sslmode=require`` connection keyword to the URL query
    (psycopg v3 reads it natively) ONLY when:
      * the host is not loopback/local (CI service container, docker-compose),
      * AND no ``sslmode`` was already specified (an operator override wins).

    Returns a SQLAlchemy ``URL`` object — passed straight to ``create_engine``
    so the password is preserved (``str(url)`` would mask it as ``***``).
    """
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        return url
    if "sslmode" in url.query:
        return url
    host = (url.host or "").strip("[]").lower()  # strip any IPv6 brackets
    if host in _LOCAL_HOSTS:
        return url
    return url.update_query_dict({"sslmode": "require"}, append=False)


def _pg_connect_args(s: Settings) -> dict[str, str | int]:
    """libpq connect args: per-connection GUC timeouts + the handshake timeout.

    The app connects as the ``postgres`` role, which — unlike Supabase's
    anon/authenticated roles — has NO server-side statement/lock/idle timeouts,
    so a connection stalled mid-transaction would hold its locks indefinitely.
    On 2026-06-18 an idle-in-transaction chunk read blocked the boot-time
    ``ALTER TABLE chunk ENABLE RLS`` and wedged prod. Setting these per
    connection makes such a stall self-heal: Postgres terminates the idle
    transaction, releases its locks, and the pool replaces the connection on the
    next checkout. Empty/``0`` values are omitted so a timeout can be disabled.

    ``connect_timeout`` (store-1) bounds the TCP/TLS handshake to the public
    Supabase pooler. The GUC ``options`` only take effect AFTER a session
    exists, and ``pool_pre_ping`` opens a fresh connection on every checkout —
    so without this keyword a stalled handshake hangs the request thread
    forever. psycopg v3 honors the libpq ``connect_timeout`` keyword natively;
    it is integer seconds, and '0'/'' omits it (handshake unbounded).
    """
    args: dict[str, str | int] = {}
    opts = [
        f"-c {guc}={value.strip()}"
        for guc, value in (
            ("statement_timeout", s.db_statement_timeout),
            ("idle_in_transaction_session_timeout", s.db_idle_in_tx_timeout),
            ("lock_timeout", s.db_lock_timeout),
        )
        if value and value.strip() and value.strip() != "0"
    ]
    if opts:
        args["options"] = " ".join(opts)
    connect_timeout = (s.db_connect_timeout or "").strip()
    if connect_timeout and connect_timeout != "0":
        # Integer seconds per libpq; a non-numeric value is operator config rot
        # — fail loudly at engine construction rather than silently unbounded.
        args["connect_timeout"] = int(connect_timeout)
    return args


def _migration_connect_args(s: Settings) -> dict[str, str]:
    """libpq options for the Alembic migration connection: ``lock_timeout`` ONLY.

    Used by migrations/env.py for remote Postgres. A migration must self-cancel
    rather than hang forever on a contended lock — the 2026-06-18 incident class,
    now reachable on the Fly ``release_command``'s one-off machine. But it must
    NOT inherit the app engine's ``statement_timeout``: a legitimately long DDL
    (e.g. a large index build) has to be allowed to finish. So bound the lock
    wait and nothing else. Empty/"0" disables it.
    """
    lock_timeout = (s.db_lock_timeout or "").strip()
    if not lock_timeout or lock_timeout == "0":
        return {}
    return {"options": f"-c lock_timeout={lock_timeout}"}


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                s = get_settings()
                # Hosted Postgres (Supabase session pooler): small pool,
                # pre-ping to survive pooler-side connection recycling, and
                # per-connection timeouts (see _pg_connect_args) so a stalled
                # transaction can never hold a lock indefinitely.
                _engine = create_engine(
                    _enforce_sslmode(_required_database_url()),
                    echo=False,
                    pool_pre_ping=True,
                    pool_size=5,
                    max_overflow=5,
                    pool_recycle=s.db_pool_recycle_s,
                    connect_args=_pg_connect_args(s),
                )
    return _engine


def engine_dialect() -> str:
    """Active SQLAlchemy dialect name (always 'postgresql' since R5).

    Kept as a function because /health reports it: a stack whose engine is
    somehow not Postgres must be visibly wrong, not silently plausible.
    """
    return get_engine().dialect.name


def _alembic_config() -> Config:
    root = _repo_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    # ConfigParser interpolation: literal '%' (percent-encoded passwords in
    # Postgres URLs) must be escaped as '%%'.
    cfg.set_main_option("sqlalchemy.url", _active_database_url().replace("%", "%%"))
    return cfg


def _head_revision(cfg: Config) -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:  # pragma: no cover - migrations/ always has a head
        raise RuntimeError("no alembic head revision found in migrations/")
    return head


def _ensure_vector_extension(engine: Engine) -> None:
    """Idempotently enable pgvector.

    Supabase ships pgvector pre-installed in the ``extensions`` schema, so
    'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions' is a no-op
    there. A local docker Postgres has no ``extensions`` schema — the plain
    form installs the extension into ``public`` instead.
    """
    with engine.begin() as conn:
        has_extensions_schema = (
            conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'")).scalar()
            is not None
        )
        if has_extensions_schema:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions"))
        else:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


def _ensure_postgres_objects(engine: Engine) -> None:
    """Idempotent DDL outside SQLModel metadata (the pgvector ``chunk`` table).

    Lock-safe like _enable_row_level_security. ``chunk`` is the hot table behind
    the 2026-06-18 lock pileup, and ``CREATE INDEX IF NOT EXISTS`` takes a
    ShareLock on it even when the index already exists — which conflicts with the
    RowExclusiveLock a concurrent ingest writer holds. So the DDL runs under a
    short ``lock_timeout`` and a contended run is logged and skipped rather than
    crash-looping the boot: every statement is ``IF NOT EXISTS`` (a skipped object
    is re-attempted on the next boot), and the only path that MUST succeed — a
    fresh empty DB — has no writer to contend with and never times out.
    """
    _ensure_vector_extension(engine)
    with engine.begin() as conn:
        vector_schema = conn.execute(
            text(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname = 'vector'"
            )
        ).scalar()
    if vector_schema is None:  # pragma: no cover - _ensure_vector_extension ran
        raise RuntimeError("pgvector extension is not installed")
    # Table first, then its indexes — all target the SAME table, so once one lock
    # is contended the rest will be too: break (don't retry each for lock_timeout)
    # to keep boot latency bounded; the next boot re-attempts the skipped objects.
    statements = (
        _CHUNK_TABLE_DDL.format(vector_schema=vector_schema),
        *(ddl.format(vector_schema=vector_schema) for ddl in _CHUNK_INDEX_DDL),
    )
    for ddl in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '3s'"))
                conn.execute(text(ddl))
        except OperationalError as exc:
            # lock_timeout -> LockNotAvailable on the contended chunk lock: leave
            # the remaining IF NOT EXISTS objects for a later boot (mirrors
            # _enable_row_level_security) instead of crashing the process.
            log.warning("ensure_postgres_objects_skipped", error=str(getattr(exc, "orig", exc)))
            break


def _enable_row_level_security(engine: Engine) -> None:
    """Enable deny-all RLS on public tables that don't already have it.

    Supabase auto-exposes public tables over its Data API (PostgREST) to the
    anon/authenticated roles; RLS-without-policies is deny-all for them. Our
    API connects as the ``postgres`` role, which bypasses RLS.

    Lock-safe and idempotent. ``ALTER TABLE`` takes an ACCESS EXCLUSIVE lock, so
    re-running it on already-protected tables under live read traffic is exactly
    what wedged prod on 2026-06-18 (the boot-time ALTER on the hot ``chunk``
    table blocked behind an idle-in-transaction reader, and a queued ACCESS
    EXCLUSIVE request in turn blocked every new reader). So:
      * skip tables that already have RLS — a steady-state boot takes NO locks;
      * run each ALTER in its own transaction under a short ``lock_timeout``;
      * if a table can't be locked right now, log and move on rather than crash
        the boot — an already-running instance keeps serving and the next boot
        re-attempts once the contended lock is free.
    """
    with engine.connect() as conn:
        pending = (
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "AND NOT c.relrowsecurity"
                )
            )
            .scalars()
            .all()
        )
    for name in pending:
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '3s'"))
                conn.execute(text(f'ALTER TABLE public."{name}" ENABLE ROW LEVEL SECURITY'))
        except OperationalError as exc:
            # lock_timeout -> LockNotAvailable: leave it for a later boot.
            log.warning("rls_enable_skipped", table=name, error=str(getattr(exc, "orig", exc)))


def _ensure_rls_event_trigger(engine: Engine) -> None:
    """(Re)create the `ensure_rls` event trigger so FUTURE tables get deny-all RLS.

    WHY here: `_enable_row_level_security` only RLSes the tables present at boot;
    migration 0011 adds an event trigger that RLSes any table created AFTERWARD.
    A migrate-replayed Postgres gets that trigger from 0011, but the fresh-boot
    path (create_all + stamp head) NEVER replays migrations, so without this it
    would be missing the trigger and silently diverge from a migrated DB. We load
    the canonical idempotent DDL from the 0011 file by path (single source of
    truth; do NOT inline it), so every bootstrap route converges on identical
    objects.

    Lock-free + idempotent (CREATE OR REPLACE FUNCTION / DROP-then-CREATE
    trigger, neither takes a table lock), so re-asserting it on the same-revision
    boot path is a safe no-op and needs no lock_timeout dance. No-op off Postgres.
    """
    if engine.dialect.name != "postgresql":
        return
    path = _repo_root() / "migrations" / "versions" / "0011_ensure_rls_event_trigger.py"
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load migration 0011 from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with engine.begin() as conn:
        for stmt in mod.rls_event_trigger_sql():
            conn.execute(text(stmt))


def _init_postgres(engine: Engine) -> None:
    """Fresh-Postgres bootstrap = create_all + stamp head (NO history replay).

    Migrations 0001-0006 contain SQLite-specific batch ops and are never
    replayed against Postgres. A stamped database must match head exactly;
    an unstamped database with existing tables is ambiguous — refuse to start.
    """
    from alembic import command

    from regwatch.store import models  # noqa: F401  (registers tables)

    cfg = _alembic_config()
    head = _head_revision(cfg)
    tables = set(inspect(engine).get_table_names())

    if "alembic_version" in tables:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        if current != head:
            exc = RuntimeError(
                f"Postgres schema is stamped at alembic revision {current!r} but this build "
                f"expects {head!r}. Refusing to start: run the data migration / upgrade "
                "tooling against this database first."
            )
            # Explicit Sentry capture point (H1): a migration-mode mismatch is
            # an operator-facing boot refusal that must be visible, not silent.
            from regwatch.common.observability import capture_exception

            capture_exception(exc)
            raise exc
        # Same revision: ensure the idempotent extras exist, then proceed.
        _ensure_postgres_objects(engine)
        _enable_row_level_security(engine)
        # Lock-free no-op re-assert: keep a same-revision boot converged with a
        # migrate-replayed DB (the trigger is the only thing 0011 adds).
        _ensure_rls_event_trigger(engine)
        return

    if tables & set(SQLModel.metadata.tables.keys()):
        exc = RuntimeError(
            "Postgres database has regwatch tables but no alembic_version stamp — "
            "ambiguous state. Refusing to start: restore from a clean database "
            "(or, if this DB was half-bootstrapped, drop its schema and re-run "
            "`regwatch init-db`)."
        )
        # Explicit Sentry capture point (H1): same migration-mode mismatch class.
        from regwatch.common.observability import capture_exception

        capture_exception(exc)
        raise exc

    # Empty database: bootstrap.
    _ensure_vector_extension(engine)
    SQLModel.metadata.create_all(engine)
    _ensure_postgres_objects(engine)
    command.stamp(cfg, "head")
    _enable_row_level_security(engine)
    # create_all + stamp head never replays 0011, so install its event trigger
    # here too -- otherwise a fresh DB lacks the auto-RLS-on-new-table guard a
    # migrate-replayed DB has. Lock-free + idempotent.
    _ensure_rls_event_trigger(engine)


def init_db() -> None:
    """Apply/verify the Postgres schema for the active database.

    Memoized per process (reset by ``reset_for_tests``): the schema work is
    idempotent but not free, and ingest calls init_db once per listing.
    """
    global _initialized
    if _initialized:
        return
    # get_engine() takes _engine_lock; call it BEFORE acquiring _init_lock.
    engine = get_engine()
    with _init_lock:
        if _initialized:
            return
        _init_postgres(engine)
        # K6 fail-fast: the embedding provider's dimension must match the
        # chunk table's vector(1536) AT STARTUP, not on first vector-store
        # use. Every entry point funnels through init_db (API lifespan,
        # `regwatch init-db`), so a misconfigured provider refuses to boot
        # instead of 500-ing on the first query/ingest.
        from regwatch.store.pgvector_store import assert_embedding_provider_dim

        assert_embedding_provider_dim()
        _initialized = True


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_for_tests() -> None:
    """Tests use this to swap in a temp DB. Resets the cached engine."""
    global _engine, _initialized
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _initialized = False
