"""pgvector-backed chunk store — the Postgres-mode twin of the Chroma store.

Active iff ``DATABASE_URL`` is set (K1 vector-backend rule): vectors live in
pgvector when the app runs against Postgres/Supabase, and in Chroma otherwise.
`store/vector_store.py` owns the public interface and dispatches here; callers
never import this module directly.

Schema (K4): one ``chunk`` table whose btree-indexed metadata columns mirror
the Chroma chunk metadata, plus a ``vector(1536)`` embedding column with an
HNSW cosine index. The table is created by the Postgres bootstrap and also
self-heals here on first use (idempotent DDL), so the store works even when
it is exercised before/without `store/db.py`'s init path.

Supabase specifics: pgvector is preinstalled in the ``extensions`` schema, so
extension creation prefers ``WITH SCHEMA extensions`` and falls back to the
default schema for a vanilla Postgres (local docker), where it lands in
``public``. Either way the unqualified ``vector`` type resolves via the
role's search_path. Row Level Security is enabled with no policies so
Supabase's auto-generated Data API (anon/authenticated roles) cannot read the
table; the app connects as ``postgres``, which bypasses RLS.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from config.settings import get_settings
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Engine, create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Field, SQLModel

from regwatch.common.logging import get_logger
from regwatch.process.embedder import get_embedding_provider

log = get_logger(__name__)

if TYPE_CHECKING:  # runtime import is deferred to avoid an import cycle
    from regwatch.store.vector_store import Hit

# The chunk table stores 1536-dim vectors (OpenAI text-embedding-3-small).
EMBEDDING_DIM = 1536

# Columns the Chroma-style `where` filters may reference (everything except
# id/text/embedding). Unknown fields fail loudly rather than silently
# returning unfiltered results — filters are a cross-drug-leak control (INV).
_FILTERABLE_COLUMNS = frozenset(
    {
        "doc_id",
        "version_id",
        "page",
        "section_path",
        "normalized_name",
        "dosage_form",
        "route",
        "source_url",
        "psg_type",
        "appl_no",
        "short_name",
    }
)
_INT_METADATA_COLUMNS = ("doc_id", "version_id", "page")
_TEXT_METADATA_COLUMNS = (
    "section_path",
    "normalized_name",
    "dosage_form",
    "route",
    "source_url",
    "psg_type",
    "appl_no",
    "short_name",
)
_METADATA_COLUMNS = _INT_METADATA_COLUMNS + _TEXT_METADATA_COLUMNS


class Chunk(SQLModel, table=True):
    """K4 chunk row. The pgvector twin of one Chroma collection entry."""

    __tablename__ = "chunk"

    id: str = Field(primary_key=True)
    doc_id: int | None = Field(default=None, index=True)
    version_id: int | None = Field(default=None, index=True)
    page: int | None = Field(default=None)
    section_path: str | None = Field(default=None)
    normalized_name: str | None = Field(default=None, index=True)
    dosage_form: str | None = Field(default=None)
    route: str | None = Field(default=None)
    source_url: str | None = Field(default=None)
    psg_type: str | None = Field(default=None)
    appl_no: str | None = Field(default=None, index=True)
    short_name: str | None = Field(default=None)
    text: str | None = Field(default=None)
    embedding: Any = Field(default=None, sa_column=Column(Vector(EMBEDDING_DIM)))


_engine: Engine | None = None
_owns_engine = False
_schema_ready = False
_metadata_values_cache: dict[str, tuple[float, frozenset[str]]] = {}


def _metadata_cache_fresh(cached_at: float) -> bool:
    """TTL gate: bounds cross-process staleness; 0 keeps the legacy behavior."""
    ttl = get_settings().metadata_cache_ttl_s
    return ttl <= 0 or (time.monotonic() - cached_at) < ttl


_INSERT_BATCH_SIZE = 1000

_UPSERT_SQL = """
INSERT INTO chunk (
    id, doc_id, version_id, page, section_path, normalized_name, dosage_form,
    route, source_url, psg_type, appl_no, short_name, text, embedding
) VALUES (
    :id, :doc_id, :version_id, :page, :section_path, :normalized_name, :dosage_form,
    :route, :source_url, :psg_type, :appl_no, :short_name, :text,
    CAST(:embedding AS vector)
)
ON CONFLICT (id) DO UPDATE SET
    doc_id = EXCLUDED.doc_id,
    version_id = EXCLUDED.version_id,
    page = EXCLUDED.page,
    section_path = EXCLUDED.section_path,
    normalized_name = EXCLUDED.normalized_name,
    dosage_form = EXCLUDED.dosage_form,
    route = EXCLUDED.route,
    source_url = EXCLUDED.source_url,
    psg_type = EXCLUDED.psg_type,
    appl_no = EXCLUDED.appl_no,
    short_name = EXCLUDED.short_name,
    text = EXCLUDED.text,
    embedding = EXCLUDED.embedding
"""


def _normalize_url(url: str) -> str:
    """Force SQLAlchemy onto the psycopg v3 driver (K1)."""
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    return url


def get_engine() -> Engine:
    """The engine for the pgvector store.

    Reuses `store/db.py`'s engine when it is already Postgres (one shared
    pool); otherwise builds its own from DATABASE_URL with the K2 pool
    parameters. The fallback keeps this module testable independent of the
    db bootstrap integration order.
    """
    global _engine, _owns_engine
    if _engine is None:
        from regwatch.store.vector_store import _database_url

        url = _database_url()
        if url is None:
            raise RuntimeError("pgvector store requires DATABASE_URL (Postgres mode)")
        from regwatch.store import db as db_module

        shared = db_module.get_engine()
        if shared.dialect.name == "postgresql":
            _engine = shared
            _owns_engine = False
        else:
            # store-1/store-7: route the fallback through the SAME hardening as
            # db.py's engine so it inherits sslmode=require (remote hosts) plus
            # the per-connection GUC timeouts AND the connect_timeout handshake
            # bound. _enforce_sslmode takes the psycopg-normalized URL and
            # returns a SQLAlchemy URL (password preserved).
            s = get_settings()
            _engine = create_engine(
                db_module._enforce_sslmode(_normalize_url(url)),
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                connect_args=db_module._pg_connect_args(s),
            )
            _owns_engine = True
    return _engine


def _ensure_extension(engine: Engine) -> None:
    """Make the `vector` type available, Supabase-style first.

    Supabase ships pgvector preinstalled in the `extensions` schema, so the
    existence check makes the whole call a no-op there. A vanilla Postgres
    (local docker) has no `extensions` schema — the `WITH SCHEMA` form fails
    and we fall back to the default schema (`public`), where the unqualified
    `vector` type resolves via the default search_path.
    """
    with engine.connect() as conn:
        installed = conn.execute(
            sa_text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    if installed:
        return
    try:
        with engine.begin() as conn:
            conn.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions"))
    except ProgrammingError:
        with engine.begin() as conn:
            conn.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector"))


def ensure_schema(engine: Engine) -> None:
    """Idempotent K4 DDL: extension, chunk table, indexes, deny-all RLS.

    Exposed for the Postgres bootstrap (`store/db.py` / migrate script) to
    call explicitly; the store also runs it lazily on first use.
    """
    _ensure_extension(engine)
    # Creates the table plus the btree indexes declared on the model
    # (doc_id, version_id, normalized_name, appl_no).
    SQLModel.metadata.tables["chunk"].create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_chunk_embedding_hnsw "
                "ON chunk USING hnsw (embedding vector_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)"
            )
        )
        # Self-heal the model-declared btree indexes too: Table.create with
        # checkfirst=True skips indexes entirely when the table already exists
        # (e.g. created first by store/db.py's bootstrap DDL), and both
        # bootstrap paths must converge on the same index set (K4) regardless
        # of module-initialization order.
        for column in ("normalized_name", "doc_id", "version_id", "appl_no"):
            conn.execute(
                sa_text(f"CREATE INDEX IF NOT EXISTS ix_chunk_{column} ON chunk ({column})")
            )
    # Deny-all for Supabase's auto-exposed Data API roles; the app's `postgres`
    # role bypasses RLS. Mandatory per the K2 contract — but enabled OUT of the
    # DDL transaction above and only when not already on, so a first-query path
    # on a live corpus takes no ACCESS EXCLUSIVE lock on the hot `chunk` table
    # (the 2026-06-18 boot deadlock). See store/db.py:_enable_row_level_security.
    _enable_chunk_rls(engine)


def _enable_chunk_rls(engine: Engine) -> None:
    """Lock-safe, idempotent deny-all RLS on `chunk` (skip if already enabled)."""
    with engine.connect() as conn:
        already = conn.execute(
            sa_text(
                "SELECT c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'chunk'"
            )
        ).scalar()
    if already:
        return
    try:
        with engine.begin() as conn:
            conn.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            conn.execute(sa_text("ALTER TABLE chunk ENABLE ROW LEVEL SECURITY"))
    except OperationalError as exc:
        # Contended ACCESS EXCLUSIVE lock: never let it wedge the first query.
        log.warning("chunk_rls_enable_skipped", error=str(getattr(exc, "orig", exc)))


def assert_embedding_provider_dim() -> None:
    """K6 fail-fast: provider dim must match the chunk table's vector(1536)."""
    provider = get_embedding_provider()
    if int(provider.dim) != EMBEDDING_DIM:
        raise RuntimeError(
            f"EMBEDDING_PROVIDER={provider.name!r} produces {provider.dim}-dim vectors, "
            f"but the Postgres chunk table stores vector({EMBEDDING_DIM}). "
            "Set EMBEDDING_PROVIDER=openai (text-embedding-3-small) in Postgres mode."
        )


def _ensure_ready() -> None:
    global _schema_ready
    if _schema_ready:
        return
    assert_embedding_provider_dim()
    ensure_schema(get_engine())
    _schema_ready = True


def reset_for_tests() -> None:
    """Drop cached engine/schema state so tests can re-point DATABASE_URL."""
    global _engine, _owns_engine, _schema_ready
    if _engine is not None and _owns_engine:
        _engine.dispose()
    _engine = None
    _owns_engine = False
    _schema_ready = False
    _metadata_values_cache.clear()


def _validate_embedding(embedding: list[float]) -> None:
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(
            f"embedding has {len(embedding)} dims; the chunk table stores vector({EMBEDDING_DIM})"
        )
    # pgvector rejects NaN/Inf inside the CAST with an opaque error far from the
    # cause; name the offending input up front, mirroring the dim check.
    if not all(math.isfinite(x) for x in embedding):
        raise ValueError("embedding contains non-finite (NaN/Inf) components")


def _vector_literal(embedding: list[float]) -> str:
    """pgvector input literal; bound as text and CAST(... AS vector) in SQL.

    String + cast avoids needing psycopg adapter registration on every pooled
    connection.
    """
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _short_name(meta: dict[str, Any]) -> str:
    """Same rule as retrieve/retriever.py — a citation-friendly doc label."""
    explicit = meta.get("short_name")
    if isinstance(explicit, str) and explicit:
        return explicit
    appl = meta.get("appl_no") or ""
    if appl:
        return f"PSG_{appl}"
    name = str(meta.get("normalized_name") or "PSG").strip()
    return name.replace(" ", "_") or "PSG"


def add_chunks(
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    if not ids:
        return
    if not (len(ids) == len(embeddings) == len(documents) == len(metadatas)):
        raise ValueError("ids, embeddings, documents, metadatas must have equal lengths")
    rows: list[dict[str, Any]] = []
    for chunk_id, embedding, document, meta in zip(
        ids, embeddings, documents, metadatas, strict=True
    ):
        _validate_embedding(embedding)
        meta = meta or {}
        rows.append(
            {
                "id": chunk_id,
                "doc_id": _as_int(meta.get("doc_id")),
                "version_id": _as_int(meta.get("version_id")),
                "page": _as_int(meta.get("page")),
                "section_path": _as_text(meta.get("section_path")),
                "normalized_name": _as_text(meta.get("normalized_name")),
                "dosage_form": _as_text(meta.get("dosage_form")),
                "route": _as_text(meta.get("route")),
                "source_url": _as_text(meta.get("source_url")),
                "psg_type": _as_text(meta.get("psg_type")),
                "appl_no": _as_text(meta.get("appl_no")),
                "short_name": _short_name(meta),
                "text": document,
                "embedding": _vector_literal(embedding),
            }
        )
    _ensure_ready()
    engine = get_engine()
    with engine.begin() as conn:
        for start in range(0, len(rows), _INSERT_BATCH_SIZE):
            conn.execute(sa_text(_UPSERT_SQL), rows[start : start + _INSERT_BATCH_SIZE])
    _metadata_values_cache.clear()


def delete_chunks_for_doc_except_version(doc_id: int, keep_version_id: int) -> int:
    """Delete indexed chunks for one PSG document except the current version.

    Same semantics as the Chroma path: a NULL/unparseable version_id never
    matches the kept version, so those rows are deleted too
    (`IS DISTINCT FROM` keeps NULLs in the delete set).
    """
    _ensure_ready()
    with get_engine().begin() as conn:
        result = conn.execute(
            sa_text(
                "DELETE FROM chunk "
                "WHERE doc_id = :doc_id AND version_id IS DISTINCT FROM :keep_version_id"
            ),
            {"doc_id": doc_id, "keep_version_id": keep_version_id},
        )
    deleted = int(result.rowcount or 0)
    if deleted:
        _metadata_values_cache.clear()
    return deleted


def _append_condition(
    column: str, value: object, conditions: list[str], params: dict[str, Any]
) -> None:
    if column not in _FILTERABLE_COLUMNS:
        raise ValueError(f"unsupported chunk filter field: {column!r}")

    def coerce(v: object) -> object:
        # Match the bound value to the column's declared type, mirroring the
        # Chroma path's loose metadata coercion: a client-supplied string
        # version_id/doc_id/page (filters is dict[str, Any] off the wire) would
        # otherwise mismatch the integer column instead of filtering correctly.
        return _as_int(v) if column in _INT_METADATA_COLUMNS else v

    if isinstance(value, dict):
        ops = set(value)
        if ops == {"$eq"}:
            param = f"p{len(params)}"
            conditions.append(f"{column} = :{param}")
            params[param] = coerce(value["$eq"])
            return
        if ops == {"$in"}:
            seq = value["$in"]
            if not isinstance(seq, list | tuple):
                raise ValueError(f"$in for {column!r} requires a list")
            if not seq:
                conditions.append("FALSE")
                return
            param = f"p{len(params)}"
            conditions.append(f"{column} = ANY(:{param})")
            params[param] = [coerce(x) for x in seq]
            return
        raise ValueError(f"unsupported filter operator(s) {ops!r} for {column!r}")
    param = f"p{len(params)}"
    conditions.append(f"{column} = :{param}")
    params[param] = coerce(value)


def _parse_where(node: dict[str, Any], conditions: list[str], params: dict[str, Any]) -> None:
    for key, value in node.items():
        if key == "$and":
            if not isinstance(value, list):
                raise ValueError("$and requires a list of clauses")
            for sub in value:
                if not isinstance(sub, dict):
                    raise ValueError("$and clauses must be dicts")
                _parse_where(sub, conditions, params)
        elif key.startswith("$"):
            raise ValueError(f"unsupported where operator: {key!r}")
        else:
            _append_condition(key, value, conditions, params)


def _where_clause(where: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Chroma-style `where` ({field: {"$eq"/"$in": ...}}, "$and") → SQL."""
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if where:
        _parse_where(where, conditions, params)
    if not conditions:
        return "", params
    return " WHERE " + " AND ".join(conditions), params


def similarity_search(
    query_embedding: list[float],
    *,
    k: int = 8,
    where: dict[str, Any] | None = None,
) -> list[Hit]:
    # Deferred import: vector_store owns Hit and never imports us at module
    # scope, so there is no cycle.
    from regwatch.store.vector_store import Hit

    if k <= 0:
        return []
    _validate_embedding(query_embedding)
    _ensure_ready()

    clause, params = _where_clause(where)
    params["qvec"] = _vector_literal(query_embedding)
    params["k"] = int(k)
    select_cols = ", ".join(("id", "text", *_METADATA_COLUMNS))
    sql = (
        f"SELECT {select_cols}, embedding <=> CAST(:qvec AS vector) AS distance "
        f"FROM chunk{clause} "
        "ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :k"
    )
    with get_engine().begin() as conn:
        if clause:
            # K5 filtered mode: exact scan over the WHERE-narrowed set. The
            # HNSW index applies metadata filters *after* the approximate
            # scan and can silently drop matches, so disable index scans
            # (bitmap scans on the btree filter columns remain available)
            # and let Postgres order the filtered rows exactly.
            conn.execute(sa_text("SET LOCAL enable_indexscan = off"))
        else:
            # K5 unfiltered mode: approximate HNSW scan, wide beam.
            conn.execute(sa_text("SET LOCAL hnsw.ef_search = 100"))
        rows = conn.execute(sa_text(sql), params).mappings().all()

    hits: list[Hit] = []
    for row in rows:
        # Score convention — MUST stay identical to the Chroma backend
        # (store/vector_store.py, similarity_search):
        #   pgvector `<=>` returns cosine distance d = 1 - cos_sim, d ∈ [0, 2],
        #   exactly what Chroma's "hnsw:space=cosine" returns.
        #   score = 1 - d/2 ∈ [0, 1]  (1.0 identical, 0.5 orthogonal, 0.0 opposite)
        # REFUSAL_SCORE_THRESHOLD (default 0.30) therefore means the same
        # thing on both backends.
        sim = 1.0 - float(row["distance"]) / 2.0
        sim = max(0.0, min(1.0, sim))
        meta = {c: row[c] for c in _METADATA_COLUMNS if row[c] is not None}
        hits.append(Hit(chunk_id=row["id"], text=row["text"] or "", metadata=meta, score=sim))
    return hits


def collection_size() -> int:
    _ensure_ready()
    with get_engine().connect() as conn:
        return int(conn.execute(sa_text("SELECT count(*) FROM chunk")).scalar() or 0)


def chunks_exist(doc_id: int, version_id: int) -> bool:
    """True iff at least one chunk row exists for (doc_id, version_id)."""
    _ensure_ready()
    with get_engine().connect() as conn:
        found = conn.execute(
            sa_text(
                "SELECT 1 FROM chunk WHERE doc_id = :doc_id " "AND version_id = :version_id LIMIT 1"
            ),
            {"doc_id": doc_id, "version_id": version_id},
        ).scalar()
    return found is not None


def distinct_metadata_values(key: str) -> set[str]:
    """Distinct non-empty values of one text metadata column.

    Mirrors the Chroma path (including the cache, invalidated by writes).
    Non-text/unknown keys return an empty set — same as a metadata key that
    no Chroma chunk carries.
    """
    cached = _metadata_values_cache.get(key)
    if cached is not None and _metadata_cache_fresh(cached[0]):
        return set(cached[1])
    if key not in _TEXT_METADATA_COLUMNS:
        # Cache the empty result too, matching the Chroma path (which caches
        # every key) so the dual-backend behavior stays identical.
        _metadata_values_cache[key] = (time.monotonic(), frozenset())
        return set()
    _ensure_ready()
    with get_engine().connect() as conn:
        rows = conn.execute(
            sa_text(f"SELECT DISTINCT {key} FROM chunk WHERE {key} IS NOT NULL AND {key} != ''")
        ).scalars()
        out = {str(v) for v in rows}
    _metadata_values_cache[key] = (time.monotonic(), frozenset(out))
    return out
