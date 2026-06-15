"""Database session management — SQLite (default) or Postgres via DATABASE_URL.

SQLite remains the dev/test default and its init path is untouched. When
``DATABASE_URL`` is set (Supabase / local docker Postgres), ``get_engine``
builds a pooled Postgres engine and ``init_db`` runs the fresh-Postgres
bootstrap: ``create_all`` + ``alembic stamp head`` — NO history replay,
because migrations 0001-0006 contain SQLite-specific batch ops.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from config.settings import get_settings
from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine

_engine: Engine | None = None
_engine_lock = Lock()
# init_db is memoized per process: idempotent but not free (inspect + DDL + RLS
# round-trips), and ingest calls it once per listing (~1,795x in a full crawl).
# A SEPARATE lock from _engine_lock — init_db calls get_engine(), which takes
# _engine_lock, so reusing it here would deadlock (Lock is not reentrant).
_init_lock = Lock()
_initialized = False
_BASELINE_TABLES = frozenset(
    {
        "product",
        "psg_document",
        "psg_version",
        "be_requirement",
        "query_log",
    }
)
_CURRENT_TABLES = _BASELINE_TABLES | frozenset(
    {
        "chat_session",
        "chat_message",
        "user",
        "auth_session",
        "ob_product",
        "ob_patent",
        "ob_exclusivity",
        "spl_document",
    }
)
_BASELINE_REVISION = "0001_initial_schema"
# Stamp targets for unstamped DBs created out-of-band by an older build's
# create_all. _init_sqlite stamps the NEWEST revision the present shape
# actually proves, then upgrades — stamping anything newer (e.g. "head")
# would silently skip the missing migrations. The probes:
#   - tables through 0005 + the 0002 query_log columns -> at least 0005;
#   - 0006's appl_type columns on ob_patent/ob_exclusivity -> 0006. 0007 is
#     only an if_not_exists index, so replaying it over a 0007-shaped DB is a
#     no-op and needs no probe of its own.
_PRE_0006_SCHEMA_REVISION = "0005_whitepaper_sources"
_CURRENT_SCHEMA_REVISION = "0006_ob_appl_type"

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
    s = get_settings()
    if s.database_url:
        return s.database_url
    return f"sqlite:///{s.sqlite_path.as_posix()}"


def _has_complete_legacy_schema() -> bool:
    """Detect pre-Alembic DBs that already have the baseline tables."""
    engine = get_engine()
    tables = set(inspect(engine).get_table_names())
    if not tables >= _BASELINE_TABLES:
        return False
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision() is None


def _has_unstamped_post_0005_schema() -> bool:
    """Detect DBs created from post-0005 SQLModel metadata before Alembic stamping.

    Verifies the tables through 0005 plus the 0002 query_log columns — i.e.
    the 0005 shape, NOT anything later. _init_sqlite probes the 0006 columns
    separately to pick the stamp the shape actually proves.
    """
    engine = get_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not tables >= _CURRENT_TABLES:
        return False
    query_columns = {c["name"] for c in inspector.get_columns("query_log")}
    if not {"session_id", "turn_id", "status", "route_json", "user_id"} <= query_columns:
        return False
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision() is None


def _has_ob_appl_type_columns() -> bool:
    """0006's shape probe: the appl_type column on ob_patent AND ob_exclusivity."""
    inspector = inspect(get_engine())
    return all(
        "appl_type" in {c["name"] for c in inspector.get_columns(t)}
        for t in ("ob_patent", "ob_exclusivity")
    )


def _matches_head_schema() -> bool:
    """True when an (unstamped) schema already carries everything past 0007.

    Discriminates a DB created from CURRENT metadata via create_all (stamp it
    head) from an older-shaped one (stamp what the shape proves, then upgrade
    so 0008's query_log token columns and answer_feedback actually land).
    """
    inspector = inspect(get_engine())
    if "answer_feedback" not in set(inspector.get_table_names()):
        return False
    query_columns = {c["name"] for c in inspector.get_columns("query_log")}
    return {"input_tokens", "output_tokens", "cost_usd"} <= query_columns


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                s = get_settings()
                if s.database_url:
                    # Hosted Postgres (Supabase session pooler): small pool,
                    # pre-ping to survive pooler-side connection recycling.
                    _engine = create_engine(
                        s.database_url,
                        echo=False,
                        pool_pre_ping=True,
                        pool_size=5,
                        max_overflow=5,
                    )
                else:
                    # B1: never fall through to SQLite in production. A missing
                    # DATABASE_URL here would otherwise boot on the container's
                    # ephemeral disk and silently destroy all data + the audit
                    # trail on the next recycle, with /health staying green.
                    if s.require_database_url:
                        raise RuntimeError(
                            "REQUIRE_DATABASE_URL is set but DATABASE_URL is empty — "
                            "refusing to boot on the SQLite fallback. Set DATABASE_URL "
                            "to the production Postgres/Supabase URL."
                        )
                    s.ensure_dirs()
                    url = f"sqlite:///{s.sqlite_path.as_posix()}"
                    _engine = create_engine(url, echo=False)
    return _engine


def engine_dialect() -> str:
    """Active SQLAlchemy dialect name ('postgresql' | 'sqlite').

    Surfaced in /health so a production stack accidentally running on SQLite is
    visibly wrong even if the B1 boot guard were ever bypassed.
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
    """Idempotent DDL that lives outside SQLModel metadata (chunk + indexes)."""
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
        conn.execute(text(_CHUNK_TABLE_DDL.format(vector_schema=vector_schema)))
        for ddl in _CHUNK_INDEX_DDL:
            conn.execute(text(ddl.format(vector_schema=vector_schema)))


def _enable_row_level_security(engine: Engine) -> None:
    """ALTER every public table to ENABLE ROW LEVEL SECURITY, with NO policies.

    Supabase auto-exposes public tables over its Data API (PostgREST) to the
    anon/authenticated roles; RLS-without-policies is deny-all for them. Our
    API connects as the ``postgres`` role, which bypasses RLS. Idempotent.
    """
    with engine.begin() as conn:
        for name in inspect(conn).get_table_names():
            conn.execute(text(f'ALTER TABLE public."{name}" ENABLE ROW LEVEL SECURITY'))


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
        return

    if tables & set(SQLModel.metadata.tables.keys()):
        exc = RuntimeError(
            "Postgres database has regwatch tables but no alembic_version stamp — "
            "ambiguous state. Refusing to start: restore from a clean database or "
            "re-run scripts/migrate_to_supabase.py with --truncate."
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


def _init_sqlite() -> None:
    """Apply schema migrations for the active SQLite database (unchanged path)."""
    from alembic import command

    from regwatch.store import models  # noqa: F401  (registers tables)

    cfg = _alembic_config()
    if _has_unstamped_post_0005_schema():
        if _matches_head_schema():
            command.stamp(cfg, "head")
        elif _has_ob_appl_type_columns():
            # 0006-shaped (0007 is an if_not_exists index — replaying it is
            # safe either way): stamp 0006 and apply 0007+.
            command.stamp(cfg, _CURRENT_SCHEMA_REVISION)
            command.upgrade(cfg, "head")
        else:
            # post-0005/pre-0006 model era: stamp only what the shape proves
            # and replay 0006+ (appl_type columns + the ob_product N->NDA
            # normalization) instead of silently skipping them.
            command.stamp(cfg, _PRE_0006_SCHEMA_REVISION)
            command.upgrade(cfg, "head")
    elif _has_complete_legacy_schema():
        command.stamp(cfg, _BASELINE_REVISION)
        command.upgrade(cfg, "head")
    else:
        command.upgrade(cfg, "head")


def init_db() -> None:
    """Apply/verify the schema for the active database (dialect-aware).

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
        if engine.dialect.name == "postgresql":
            _init_postgres(engine)
            # K6 fail-fast: the embedding provider's dimension must match the
            # chunk table's vector(1536) AT STARTUP, not on first vector-store
            # use. Every Postgres-mode entry point funnels through init_db (API
            # lifespan, `regwatch init-db`, scripts/migrate_to_supabase.py), so
            # a misconfigured provider refuses to boot instead of 500-ing on
            # the first query/ingest. Imported lazily so SQLite mode never
            # touches the pgvector module.
            from regwatch.store.pgvector_store import assert_embedding_provider_dim

            assert_embedding_provider_dim()
        else:
            _init_sqlite()
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
