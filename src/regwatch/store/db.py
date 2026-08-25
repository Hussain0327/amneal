"""Database session management — Postgres via DATABASE_URL (the only mode).

Postgres-only since R5 (docs/POLYGLOT_TARGET_2026-07-10.md): DATABASE_URL is
mandatory and ``get_engine`` refuses to build anything else — the SQLite
fallback that once served dev/tests is gone (tests run against a disposable
Postgres named by TEST_DATABASE_URL). ``init_db`` runs the fresh-Postgres
bootstrap: ``create_all`` + ``alembic stamp head`` — NO history replay,
because migrations 0001-0006 contain SQLite-specific batch ops from the
pre-Postgres era.
"""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from config.settings import Settings, get_settings
from sqlalchemy import Engine, event, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import DBAPIError, InvalidatePoolError
from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection
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
# Two flags, not one. The schema work and the provider assert are memoized
# SEPARATELY so a bootstrap caller that opts out of the assert
# (init_db(assert_provider=False)) cannot mark the process "initialized" and
# thereby suppress the assert for a later serving-path caller in the same
# process. Collapsing these back into one flag reintroduces that hole.
_schema_ready = False
_provider_asserted = False
# Public tables observed WITHOUT row level security after the last RLS sweep.
# Boot deliberately TOLERATES a skipped ALTER (see _enable_row_level_security),
# so this is how that skip stops being silent: /ready fails closed while this is
# non-empty. Written only by _record_unprotected_tables; read via
# unprotected_public_tables().
_unprotected_tables: tuple[str, ...] = ()
# Tables PROVEN to exist in this process (see table_exists). Presence only: a
# table can appear under a running process, but nothing drops one under a live
# app, so a cached True stays true while a cached False would latch a hot
# process into the catalog-absent branch forever.
_known_tables: set[str] = set()

# K4: the pgvector chunk store. The embedding column is raw DDL (the `vector`
# type comes from the pgvector extension); everything else is the chunk
# metadata the vector-store facade (store/vector_store.py) exposes.
# {vector_schema} is the schema the extension actually lives in — `extensions`
# on Databricks Lakebase, `public` on a local docker Postgres. Qualifying the
# type and the opclass means the DDL works even when that schema isn't on the
# connection's search_path. Lakebase's DEFAULT search_path is `"$user", public`
# and does NOT include `extensions`, so this qualification is load-bearing
# there — the app role carries it via `ALTER ROLE ... SET search_path`, but
# migrations and bootstrap DDL must not depend on that.
_CHUNK_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS chunk (
    id TEXT PRIMARY KEY,
    doc_id INTEGER,
    version_id INTEGER,
    fda_document_id INTEGER REFERENCES fda_document(id),
    fda_version_id INTEGER REFERENCES fda_document_version(id),
    ordinal INTEGER,
    page INTEGER,
    section_path TEXT,
    normalized_name TEXT,
    dosage_form TEXT,
    route TEXT,
    source_url TEXT,
    psg_type TEXT,
    appl_no TEXT,
    short_name TEXT,
    source_family TEXT,
    document_type TEXT,
    locator TEXT,
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
    # Renamed by migration 0026: this index is on the LEGACY chunk.embedding
    # column, not on chunk_embedding, and the old name said otherwise. The DDL
    # here must use the post-0026 name or every boot recreates the old one
    # alongside the renamed index -- two HNSW indexes on one column, ~42 MB of
    # duplicate against a 512 MiB branch cap.
    "CREATE INDEX IF NOT EXISTS ix_chunk_embedding_hnsw ON chunk "
    "USING hnsw (embedding {vector_schema}.vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_normalized_name ON chunk (normalized_name)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_doc_id ON chunk (doc_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_version_id ON chunk (version_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_fda_document_id ON chunk (fda_document_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_fda_version_id ON chunk (fda_version_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_source_family ON chunk (source_family)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_document_type ON chunk (document_type)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_appl_no ON chunk (appl_no)",
)

# SQLSTATEs where the ENVIRONMENT refused a runtime DDL statement rather than the
# statement itself being wrong:
#   55P03 lock_not_available     - the contended lock of the 2026-06-18 incident;
#   42501 insufficient_privilege - a role without CREATE on the schema / without
#                                  ownership of `chunk` (the least-privilege DB
#                                  creds this app is moving to), or any
#                                  non-superuser hitting CREATE EVENT TRIGGER;
#   25006 read_only_transaction  - a read-only role or a replica in recovery
#                                  (docs/POLYGLOT_TARGET_2026-07-10.md:158 makes
#                                  a read-only Python role the end state).
# All three are operator-fixable or transient and every runtime DDL statement is
# idempotent (IF NOT EXISTS / CREATE OR REPLACE / skip-if-already-on), so the
# object is re-attempted on the next boot or first use. Any OTHER SQLSTATE
# (42601 syntax_error, 42P01 undefined_table, 42703 undefined_column, ...) means
# OUR DDL is wrong: those must still crash rather than boot a half-built schema.
_DEGRADABLE_DDL_SQLSTATES = {
    "55P03": "lock_not_available",
    "42501": "insufficient_privilege",
    "25006": "read_only_transaction",
    # 57014 is here to preserve the pre-existing `except OperationalError`
    # behavior, NOT as a new tolerance: psycopg maps class-57 QueryCanceled to
    # OperationalError, so the old guards already swallowed it. Boot DDL runs on
    # the APP engine, which applies statement_timeout (get_engine -> ...
    # _pg_connect_args; default 30s), unlike the migration engine that
    # deliberately omits it so a long index build can finish. So a re-attempted
    # HNSW build on a populated `chunk` table times out at 30s -- exactly the
    # state this design creates by skipping objects -- and without this entry
    # that would crash-loop boot: the failure class this guard exists to remove.
    "57014": "query_canceled",
}


def ddl_degrade_reason(exc: DBAPIError) -> str | None:
    """Name the environmental reason this DDL failure may be skipped, else None.

    Shared with ``pgvector_store`` so the boot-time and lazy-first-use DDL paths
    can never drift apart on what counts as skippable. A driver error with no
    SQLSTATE (a dead connection) is deliberately NOT degradable.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return _DEGRADABLE_DDL_SQLSTATES.get(str(sqlstate or ""))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _active_database_url() -> str:
    return _required_database_url()


def _required_database_url() -> str:
    """DATABASE_URL, or a loud refusal -- there is no fallback datastore.

    Preserves the B1 fail-loud posture unconditionally: an unset
    DATABASE_URL is an error at first engine use, never a silent fallback.
    """
    url = (get_settings().database_url or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is empty -- Postgres is the only datastore and "
            "there is no fallback. Set DATABASE_URL to a "
            "Postgres URL (Databricks Lakebase in prod); tests use "
            "TEST_DATABASE_URL (see tests/conftest.py)."
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

    The Lakebase endpoint is reached over the PUBLIC internet
    (ep-<id>.database.<region>.cloud.databricks.com), so the connection MUST be
    encrypted.
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

    The app connects as a dedicated owner role (``regwatch_app`` on Lakebase),
    which ships with NO server-side statement/lock/idle timeouts, so a
    connection stalled mid-transaction would hold its locks indefinitely.
    On 2026-06-18 an idle-in-transaction chunk read blocked the boot-time
    ``ALTER TABLE chunk ENABLE RLS`` and wedged prod. Setting these per
    connection makes such a stall self-heal: Postgres terminates the idle
    transaction, releases its locks, and the pool replaces the connection on the
    next checkout. Empty/``0`` values are omitted so a timeout can be disabled.

    ``connect_timeout`` (store-1) bounds the TCP/TLS handshake to the public
    Lakebase endpoint. The GUC ``options`` only take effect AFTER a session
    exists, and the checkout liveness listener (_register_pool_idle_ping) opens
    a fresh connection whenever its ping fails, so without this keyword a
    stalled handshake hangs the request thread forever. psycopg v3 honors the
    libpq ``connect_timeout`` keyword natively; it is integer seconds, and
    '0'/'' omits it (handshake unbounded).

    ``keepalives*`` bound the other stall: a connection PARKED in the pool whose
    peer vanished silently. See the block that sets them.

    Note the case none of this covers, unchanged by the move off pool_pre_ping:
    ``idle_in_transaction_session_timeout`` (60 s, the 2026-06-18 fix) lets the
    server kill a connection that is CHECKED OUT. No checkout-time liveness
    check ever saw that -- pre-ping did not either. It is detected by
    ``is_disconnect`` on the next statement, and the broken connection is
    discarded rather than parked, so it can never be handed to a later checkout.
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
    if s.db_keepalives_idle_s > 0:
        # A pooled connection can sit parked for db_pool_recycle_s (1800 s) on
        # the public Fly -> Lakebase path. TCP keepalives hold NAT/firewall
        # state open and make the kernel declare a vanished peer dead within
        # idle + 3*10 s, so the next ping or statement fails immediately instead
        # of blocking a request thread on TCP retransmission.
        args["keepalives"] = 1
        args["keepalives_idle"] = s.db_keepalives_idle_s
        args["keepalives_interval"] = 10
        args["keepalives_count"] = 3
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


# Key under which the checkin timestamp is parked in ConnectionPoolEntry.info.
# That dict lives exactly as long as the underlying DBAPI connection --
# SQLAlchemy clears it whenever a connection is replaced -- which is why a
# MISSING stamp safely means "brand new connection, cannot be stale".
_POOL_IDLE_STAMP = "_rw_pool_last_checkin"


def _ping_pooled_connection(engine: Engine, dbapi_connection: DBAPIConnection) -> None:
    """Round-trips one pooled connection, or raises to force a reconnect.

    Args:
        engine: Engine whose dialect issues the ping (``SELECT 1`` on psycopg).
        dbapi_connection: The raw pooled connection about to be handed out.

    Raises:
        InvalidatePoolError: The connection did not answer. Deliberate parity
            with what ``pool_pre_ping`` itself raised, so ONE failed ping
            discards the whole stale pool generation instead of every other
            parked connection failing in turn at its own checkout.
    """
    try:
        alive = engine.dialect.do_ping(dbapi_connection)
    except Exception as exc:
        # Dialect.do_ping RAISES on a dead socket (it is the private
        # _do_ping_w_event wrapper that swallows disconnects into False), and a
        # non-DBAPI error escaping here would poison every checkout in the
        # process. Convert everything into the reconnect path.
        raise InvalidatePoolError() from exc
    if not alive:
        raise InvalidatePoolError()


def _register_pool_idle_ping(engine: Engine, idle_ping_s: int) -> None:
    """Arms a checkout liveness ping gated on how long the connection idled.

    The replacement for ``pool_pre_ping``: stamp a monotonic timestamp on
    checkin, and on checkout ping only when the connection has been parked for
    at least ``idle_ping_s``. Every checkout inside one request follows its
    checkin by milliseconds, so the pings collapse to at most one per lull.

    Registering a checkout listener is what preserves the old safety net:
    SQLAlchemy arms its checkout retry loop when the pool has a checkout
    listener OR ``_pre_ping``, so a raised InvalidatePoolError still invalidates
    the pool generation and transparently reconnects BEFORE the caller's first
    statement.

    30 s is sized against the most aggressive cutoff the platform allows, not
    against a measured one: Lakebase guarantees a 24 h idle timeout but suspends
    a scale-to-zero endpoint after as little as 60 s of inactivity, and a
    connection used 30 s ago proves the endpoint was awake 30 s ago.

    Args:
        engine: Engine to attach the pool listeners to. Attach BEFORE the engine
            is published, or a concurrent checkout runs unguarded.
        idle_ping_s: Idle seconds required before a checkout pings. 0 or less
            pings on every checkout -- exactly the old pool_pre_ping behavior,
            and the rollback that needs no deploy.
    """

    @event.listens_for(engine, "checkin")
    def _stamp_idle_start(
        dbapi_connection: DBAPIConnection | None,
        connection_record: ConnectionPoolEntry,
    ) -> None:
        # checkin dispatches with a None connection when the record was already
        # invalidated; there is nothing being parked in that case.
        if dbapi_connection is not None:
            connection_record.info[_POOL_IDLE_STAMP] = time.monotonic()

    @event.listens_for(engine, "checkout")
    def _ping_when_idle(
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
        connection_proxy: PoolProxiedConnection,
    ) -> None:
        # Total by construction -- a dict get, a float compare, one call -- so
        # nothing but the ping itself can raise out of a checkout.
        last_checkin = connection_record.info.get(_POOL_IDLE_STAMP)
        # A missing stamp means this DBAPI connection was created by this very
        # checkout, so it cannot have gone stale.
        if last_checkin is not None and time.monotonic() - float(last_checkin) >= idle_ping_s:
            _ping_pooled_connection(engine, dbapi_connection)
        # Re-stamp as a fallback: if checkin never fires (the record is
        # invalidated mid-scope) an ancient timestamp must not survive.
        connection_record.info[_POOL_IDLE_STAMP] = time.monotonic()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                s = get_settings()
                # Hosted Postgres (Lakebase DIRECT endpoint, never the
                # `-pooler` host, which is PgBouncer transaction mode): small
                # pool, and per-connection timeouts (see _pg_connect_args) so a
                # stalled transaction can never hold a lock indefinitely.
                #
                # Liveness is NOT pool_pre_ping. That paid a full round trip on
                # EVERY checkout: 24 of one traced ask turn's 61 round trips,
                # 575 ms of its 5.4 s, spent proving a connection returned
                # milliseconds earlier was still alive. _register_pool_idle_ping
                # keeps the same check but gates it on actual idleness, and a
                # checkout listener arms the identical reconnect machinery
                # pre-ping used. pgx (the Go edge fronting this same endpoint)
                # has shipped this policy as its default since v5.
                #
                # pool_use_lifo keeps a serial workload on ONE hot connection
                # rather than rotating through five that each go cold, which is
                # what makes the idle gate actually skip.
                engine = create_engine(
                    _enforce_sslmode(_required_database_url()),
                    echo=False,
                    pool_size=5,
                    max_overflow=5,
                    pool_use_lifo=True,
                    pool_recycle=s.db_pool_recycle_s,
                    connect_args=_pg_connect_args(s),
                )
                _register_pool_idle_ping(engine, s.db_pool_idle_ping_s)
                # Publish only once the listeners are attached, still inside
                # _engine_lock, so no thread can check out from an unlistened
                # engine.
                _engine = engine
    return _engine


def engine_dialect() -> str:
    """Active SQLAlchemy dialect name (always 'postgresql' since R5).

    Kept as a function because /health reports it: a stack whose engine is
    somehow not Postgres must be visibly wrong, not silently plausible.
    """
    return get_engine().dialect.name


def table_exists(name: str) -> bool:
    """Whether ``name`` exists, caching PRESENCE only and never absence.

    The asymmetry is deliberate. Tables appear under a running process (the
    fresh-Postgres create_all + stamp-head bootstrap, test fixtures), so a
    negative must stay live and keep probing; tables are never dropped under a
    live app, so a positive can be answered from memory, and a stale positive
    only produces a loud SQL error. A cached negative is the direction that
    causes harm: for ``psg_document`` it would silently disable current-version
    scoping in retrieval and let superseded PSG chunks be cited.

    Args:
        name: Unqualified table name to probe in the active database.

    Returns:
        True if the table exists now, or was observed at any earlier point in
        this process; False otherwise.
    """
    if name in _known_tables:
        return True
    if not inspect(get_engine()).has_table(name):
        return False
    _known_tables.add(name)
    return True


def _alembic_config() -> Config:
    root = _repo_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    # ConfigParser interpolation: literal '%' (percent-encoded passwords in
    # Postgres URLs) must be escaped as '%%'.
    cfg.set_main_option("sqlalchemy.url", _active_database_url().replace("%", "%%"))
    return cfg


def prepare_release_database() -> None:
    """Migrate to head, then assert that the serving process can cold-boot.

    Fly runs this in its one-off release machine before touching any long-lived
    machine. The ordering is load-bearing: the existing schema stamp may be
    behind this image, so the migration must run before :func:`init_db`; after
    the migration, the normal serving guard verifies the exact head plus the
    active embedding profile's coverage, configured HNSW policy, and provider
    geometry.

    Keeping that second phase in the release machine prevents deterministic
    serving-state drift from surfacing five minutes later as a rolling-update
    health timeout after some process groups have already been replaced.
    """
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    init_db()


def _head_revision(cfg: Config) -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:  # pragma: no cover - migrations/ always has a head
        raise RuntimeError("no alembic head revision found in migrations/")
    return head


def _ensure_vector_extension(engine: Engine) -> None:
    """Idempotently enable pgvector.

    Lakebase ships pgvector (0.8.0) pre-installed in the ``extensions`` schema,
    so 'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions' is a no-op
    there. A local docker Postgres has no ``extensions`` schema — the plain
    form installs the extension into ``public`` instead.

    Unlike every other DDL site this one has NO degrade guard, by design: a
    genuinely absent extension must stay a hard boot refusal (``create_all``
    cannot build the ``vector`` column without it). So it must not ASK when the
    answer cannot change anything - Postgres checks read-only status and
    privilege BEFORE the IF NOT EXISTS existence check, so the un-probed
    statement raises (25006 on a read-only role/replica, 42501 for a
    non-owner) even though there is nothing to do. Mirrors the same probe in
    ``pgvector_store._ensure_extension``.
    """
    with engine.connect() as conn:
        installed = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    if installed is not None:
        return
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
        except DBAPIError as exc:
            # The environment refused the statement (contended chunk lock, a role
            # without DDL privilege, a read-only session): leave the remaining
            # IF NOT EXISTS objects for a later boot (mirrors
            # _enable_row_level_security) instead of crashing the process. A
            # broken statement of ours still raises. `reason` keeps a transient
            # lock distinguishable from a permanent privilege gap in the logs.
            reason = ddl_degrade_reason(exc)
            if reason is None:
                raise
            log.warning(
                "ensure_postgres_objects_skipped",
                reason=reason,
                error=str(getattr(exc, "orig", exc)),
            )
            break
    # Profile tables are registered in SQLModel.metadata for fresh create_all
    # and created by migration 0015 on upgrades.  Their trigger functions are
    # outside metadata, so converge both bootstrap routes here.  The helper
    # first checks catalog state and takes no table lock on a healthy boot.
    _ensure_embedding_profile_objects(engine)


def _ensure_embedding_profile_objects(engine: Engine) -> None:
    """Install migration 0015's immutable/dimension/invalidation triggers.

    Like the 0011 event-trigger convergence helper, the canonical DDL stays in
    its migration.  Fresh Postgres is create_all + stamp-head and never replays
    0015, so it needs the trigger objects installed explicitly.  A migrated or
    steady-state database returns after catalog checks without re-taking locks.
    """
    if engine.dialect.name != "postgresql":
        return
    with engine.connect() as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' "
                    "AND tablename IN ('chunk', 'embedding_profile', 'chunk_embedding')"
                )
            )
        }
        trigger_names = {
            str(row[0])
            for row in conn.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgname IN ("
                    "'embedding_profile_immutable', "
                    "'chunk_embedding_profile_dimension', "
                    "'chunk_text_invalidates_profile_embeddings')"
                )
            )
        }
        vector_schema = conn.execute(
            text(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname = 'vector'"
            )
        ).scalar()
    if tables != {"chunk", "embedding_profile", "chunk_embedding"}:
        return
    expected_triggers = {
        "embedding_profile_immutable",
        "chunk_embedding_profile_dimension",
        "chunk_text_invalidates_profile_embeddings",
    }
    if trigger_names == expected_triggers:
        return

    path = _repo_root() / "migrations" / "versions" / "0015_embedding_profiles.py"
    spec = importlib.util.spec_from_file_location("migration_0015", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load migration 0015 from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if vector_schema is None:  # pragma: no cover - chunk requires the extension
        raise RuntimeError("pgvector extension is not installed")
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '3s'"))
            for statement in mod.embedding_profile_support_sql(str(vector_schema)):
                conn.execute(text(statement))
    except DBAPIError as exc:
        # The chunk text trigger requires a lock on the hot chunk table, and the
        # CREATE FUNCTION/TRIGGER needs DDL privilege.  Match the rest of boot:
        # skip when the environment refuses it and retry on a later boot.
        reason = ddl_degrade_reason(exc)
        if reason is None:
            raise
        log.warning(
            "embedding_profile_objects_skipped",
            reason=reason,
            error=str(getattr(exc, "orig", exc)),
        )


def _enable_row_level_security(engine: Engine) -> None:
    """Enable deny-all RLS on public tables that don't already have it.

    Lakebase offers a PostgREST-compatible Data API over public tables;
    RLS-without-policies is deny-all for any such caller. Our API connects as
    ``regwatch_app``, which holds BYPASSRLS —
    that attribute is REQUIRED, since a grant-only role connects fine and then
    reads zero rows, booting healthy while refusing every question.

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

    A tolerant boot is NOT a silent one: the sweep re-checks itself and publishes
    whatever it failed to protect (_record_unprotected_tables), which is what
    /ready gates on.
    """
    pending = _public_tables_without_rls(engine)
    for name in pending:
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '3s'"))
                conn.execute(text(f'ALTER TABLE public."{name}" ENABLE ROW LEVEL SECURITY'))
        except DBAPIError as exc:
            # Contended lock, no privilege to ALTER, read-only session: leave it
            # for a later boot. Swallowing here is ONLY safe because
            # _record_unprotected_tables below still runs and publishes the
            # table to /ready -- an escaping exception would skip that and hand
            # out a clean bill of health over an anon-readable table.
            reason = ddl_degrade_reason(exc)
            if reason is None:
                raise
            log.warning(
                "rls_enable_skipped",
                table=name,
                reason=reason,
                error=str(getattr(exc, "orig", exc)),
            )
    _record_unprotected_tables(engine)


def _public_tables_without_rls(engine: Engine) -> list[str]:
    """Tables the RLS sweep targets that currently have RLS OFF.

    ONE definition shared by the sweep (its pending list) and the post-sweep
    verification, so the readiness gate can never disagree with what boot tried
    to fix. The criteria mirror what a PostgREST Data API actually exposes:
    schema ``public`` + ``relkind = 'r'`` (ordinary tables; views, partitioned
    parents and foreign tables are out of scope for both). Bookkeeping tables
    are NOT exempt -- ``alembic_version`` is a plain public table, so the sweep
    already RLSes it and it is a true positive here rather than noise.
    """
    with engine.connect() as conn:
        return [
            str(name)
            for name in conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "AND NOT c.relrowsecurity"
                )
            )
            .scalars()
            .all()
        ]


def _record_unprotected_tables(engine: Engine) -> None:
    """Publish the still-unprotected set for /ready and report it to Sentry.

    WHY this instead of raising on a skipped ALTER: the swallow above is the
    DELIBERATE post-incident design from 2026-06-18 -- crash-looping boot on a
    contended lock is exactly what wedged prod. So boot stays tolerant and
    READINESS goes strict: a machine still carrying an anon-readable public
    table reports /ready not_ready until a later boot's ALTER lands, and an
    operator hears about it through the same explicit capture point the stamp
    guard uses -- instead of the previous only-detection, a manual dashboard
    Advisors check in the deploy smoke list. Note fly.toml checks /health, not
    /ready, so the Sentry capture below is the alerting path; /ready is the
    machine-level signal, not a Fly routing gate.
    """
    global _unprotected_tables
    try:
        remaining = tuple(sorted(_public_tables_without_rls(engine)))
    except Exception as exc:
        # The verification query itself failed (DB gone mid-boot). Do NOT clear a
        # previously recorded bad set on an unreadable catalog -- that would hand
        # out a clean bill of health we never confirmed. /ready's db check owns
        # the unreachable case.
        log.warning("rls_verify_failed", error=str(exc))
        return
    _unprotected_tables = remaining
    if not remaining:
        return
    log.error("rls_unprotected_tables", tables=list(remaining))
    from regwatch.common.observability import capture_exception

    capture_exception(
        RuntimeError(
            "public tables without row level security after the boot sweep: "
            + ", ".join(remaining)
            + " -- readable by an unprivileged caller over the Data API. "
            "This machine reports /ready not_ready until a later boot's ALTER "
            "succeeds."
        )
    )


def unprotected_public_tables() -> tuple[str, ...]:
    """Public tables the last RLS sweep left unprotected (empty = all protected).

    Also empty when no sweep has run in this process yet: the readiness gate
    reports only what it has actually observed, never a guess.
    """
    return _unprotected_tables


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

    PRIVILEGE is the failure mode here, not locks: CREATE EVENT TRIGGER requires
    superuser, so any lesser role is refused (42501) on EVERY boot. That must
    not crash-loop the machine, but unlike a contended lock it never self-heals,
    so the degrade is loud (log.error + Sentry) and the accepted cost is
    explicit: without the trigger a table created AFTER boot can stay un-RLSed
    until the next boot's sweep sees it and /ready fails closed.
    """
    if engine.dialect.name != "postgresql":
        return
    path = _repo_root() / "migrations" / "versions" / "0011_ensure_rls_event_trigger.py"
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load migration 0011 from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        with engine.begin() as conn:
            for stmt in mod.rls_event_trigger_sql():
                conn.execute(text(stmt))
    except DBAPIError as exc:
        reason = ddl_degrade_reason(exc)
        if reason is None:
            raise
        log.error(
            "rls_event_trigger_skipped",
            reason=reason,
            error=str(getattr(exc, "orig", exc)),
        )
        from regwatch.common.observability import capture_exception

        # Capture the driver error itself: its SQLSTATE and message are what an
        # operator needs to decide between granting the privilege and accepting
        # the gap (same explicit capture-point style as the stamp guard below).
        capture_exception(exc)


def _init_postgres(engine: Engine) -> None:
    """Fresh-Postgres bootstrap = create_all + stamp head (NO history replay).

    Migrations 0001-0006 contain SQLite-specific batch ops and are never
    replayed against Postgres. A stamped database must match head exactly;
    an unstamped database with existing tables is ambiguous — refuse to start.
    """
    from alembic import command

    # Register both ORM tables and the additive profile/core tables before the
    # fresh-Postgres create_all + stamp-head path.  pgvector_store registers
    # ``chunk`` itself, which chunk_embedding's foreign key references.
    # graph_store was missing here from 0018's landing: a fresh bootstrap
    # stamped head WITHOUT the three graph tables, so any later downgrade (or
    # graph write) hit missing relations. Registering it restores the
    # create_all-equals-migration-replay convergence contract.
    from regwatch.store import (  # noqa: F401
        deficiency_kb,
        embedding_profiles,
        graph_store,
        models,
        pgvector_store,
    )

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


def init_db(*, assert_provider: bool = True) -> None:
    """Apply/verify the Postgres schema for the active database.

    Memoized per process (reset by ``reset_for_tests``): the schema work is
    idempotent but not free, and ingest calls init_db once per listing.

    ``assert_provider=False`` applies the schema WITHOUT the K6 serving-path
    provider check. It exists for offline bootstrap and corpus-maintenance
    commands that necessarily run before a serving-ready provider can exist:

      * ``embedding-profile-register`` mints the profile id, so it cannot name
        an active embedding profile that does not exist yet. The maintenance
        path still validates the selected profile before writing vectors.
      * ``embedding-profile-index`` BUILDS the HNSW index that
        assert_profile_ready_for_activation requires be already built.
      * authoritative corpus sync creates pending chunks; status diagnoses
        them; and the embedding backfill repairs them. Requiring complete
        global coverage at the entrance to any of those commands would make a
        partial run unrecoverable. The selected immutable profile is still
        validated when the backfill constructs its embedder.
      * ``authoritative-corpus-init-db`` gives the private Dagster worker the
        same schema/RLS guard without importing serving readiness into the
        maintenance control plane.

    None of these paths serves retrieval. Every serving caller keeps the
    fail-fast: pass this flag only for a command that performs maintenance and
    validates any provider at its actual write boundary.
    """
    global _schema_ready, _provider_asserted
    if _schema_ready and (_provider_asserted or not assert_provider):
        return
    # get_engine() takes _engine_lock; call it BEFORE acquiring _init_lock.
    engine = get_engine()
    with _init_lock:
        if not _schema_ready:
            _init_postgres(engine)
            _schema_ready = True
        # K6 fail-fast: the embedding provider's dimension must match the
        # chunk table's vector(1536) AT STARTUP, not on first vector-store
        # use. Every entry point funnels through init_db (API lifespan,
        # `regwatch init-db`), so a misconfigured provider refuses to boot
        # instead of 500-ing on the first query/ingest.
        if assert_provider and not _provider_asserted:
            from regwatch.store.pgvector_store import assert_embedding_provider_dim

            assert_embedding_provider_dim()
            _provider_asserted = True


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yields a Session: commit on clean exit, rollback and re-raise on error.

    Deliberately has NO retry, and must not grow one. The ONLY retry in this
    stack lives inside SQLAlchemy's checkout (see _register_pool_idle_ping),
    strictly BEFORE this scope's first statement -- that placement is what makes
    it idempotent, because a reconnect there cannot replay work that has not run
    yet. Re-entering this with-body would replay whatever it already committed
    (a single ask turn commits ~10 times), so a disconnect detected mid-scope
    must surface to the caller instead. A @contextmanager structurally cannot
    re-enter its body, so the invariant holds as long as nobody wraps callers in
    a blanket retry.

    The rollback below cannot mask that error: SQLAlchemy skips the actual
    ROLLBACK once the pooled connection is known invalid, so the original
    OperationalError (connection_invalidated=True) is what propagates.

    Yields:
        A Session bound to the process engine. Always closed.
    """
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
    global _engine, _schema_ready, _provider_asserted, _unprotected_tables
    if _engine is not None:
        _engine.dispose()
    _engine = None
    # BOTH flags: leaving _provider_asserted set would carry an assertion made
    # against the previous database into the next one.
    _schema_ready = False
    _provider_asserted = False
    # The recorded set belongs to the database being swapped out; carrying it
    # into the next one would gate /ready on a finding from a different DB.
    _unprotected_tables = ()
    # Every schema-dropping fixture routes through here, and the observations
    # in this set belong to the database being swapped out.
    _known_tables.clear()
    from regwatch.store import embedding_profiles

    embedding_profiles.reset_for_tests()
