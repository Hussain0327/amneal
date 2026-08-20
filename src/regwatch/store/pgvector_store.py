"""pgvector-backed chunk store — the only vector backend since R5.

`store/vector_store.py` owns the public interface and delegates here; callers
never import this module directly.

Schema (K4): one ``chunk`` table with btree-indexed metadata columns (the
citation and drug-filter fields), plus a ``vector(1536)`` embedding column
with an HNSW cosine index. The table is created by the Postgres bootstrap and also
self-heals here on first use (idempotent DDL), so the store works even when
it is exercised before/without `store/db.py`'s init path.

Lakebase specifics: pgvector is preinstalled in the ``extensions`` schema, so
extension creation prefers ``WITH SCHEMA extensions`` and falls back to the
default schema for a vanilla Postgres (local docker), where it lands in
``public``. Either way the unqualified ``vector`` type resolves via the
role's search_path — on Lakebase that requires ``ALTER ROLE <role> SET
search_path TO public, extensions``, since the server default omits
``extensions``. Row Level Security is enabled with no policies so a
PostgREST-style Data API caller cannot read the table; the app connects as
``regwatch_app``, which holds BYPASSRLS.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from config.settings import get_settings
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Connection, Engine
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlmodel import Field, SQLModel

from regwatch.common.logging import get_logger
from regwatch.process.embedder import (
    Qwen3EmbeddingProvider,
    get_embedding_provider,
    get_embedding_provider_for_profile,
)
from regwatch.store.db import ddl_degrade_reason

log = get_logger(__name__)

if TYPE_CHECKING:  # runtime import is deferred to avoid an import cycle
    from regwatch.store.vector_store import Hit

# The chunk table stores 1536-dim vectors (OpenAI text-embedding-3-small).
EMBEDDING_DIM = 1536

# Columns the `where` filter DSL may reference (everything except
# id/text/embedding). Unknown fields fail loudly rather than silently
# returning unfiltered results — filters are a cross-drug-leak control (INV).
_FILTERABLE_COLUMNS = frozenset(
    {
        "doc_id",
        "version_id",
        "fda_document_id",
        "fda_version_id",
        "page",
        "section_path",
        "normalized_name",
        "dosage_form",
        "route",
        "source_url",
        "psg_type",
        "appl_no",
        "short_name",
        "source_family",
        "document_type",
        "locator",
    }
)
_INT_METADATA_COLUMNS = ("doc_id", "version_id", "fda_document_id", "fda_version_id", "page")
_TEXT_METADATA_COLUMNS = (
    "section_path",
    "normalized_name",
    "dosage_form",
    "route",
    "source_url",
    "psg_type",
    "appl_no",
    "short_name",
    "source_family",
    "document_type",
    "locator",
)
_METADATA_COLUMNS = _INT_METADATA_COLUMNS + _TEXT_METADATA_COLUMNS


class Chunk(SQLModel, table=True):
    """K4 chunk row: text, citation/filter metadata, and the legacy vector."""

    __tablename__ = "chunk"

    id: str = Field(primary_key=True)
    doc_id: int | None = Field(default=None, index=True)
    version_id: int | None = Field(default=None, index=True)
    # Generic authoritative-corpus identity. PSG compatibility stays in the
    # legacy doc_id/version_id pair while the new source universe rolls out.
    fda_document_id: int | None = Field(default=None, foreign_key="fda_document.id", index=True)
    fda_version_id: int | None = Field(
        default=None, foreign_key="fda_document_version.id", index=True
    )
    # In-document ordering (0017). The chunker always stamps it; NULL only on
    # rows written before the column existed. Kept out of _FILTERABLE_COLUMNS:
    # it orders siblings, it is not a retrieval filter.
    ordinal: int | None = Field(default=None)
    page: int | None = Field(default=None)
    section_path: str | None = Field(default=None)
    normalized_name: str | None = Field(default=None, index=True)
    dosage_form: str | None = Field(default=None)
    route: str | None = Field(default=None)
    source_url: str | None = Field(default=None)
    psg_type: str | None = Field(default=None)
    appl_no: str | None = Field(default=None, index=True)
    short_name: str | None = Field(default=None)
    source_family: str | None = Field(default=None, index=True)
    document_type: str | None = Field(default=None, index=True)
    locator: str | None = Field(default=None)
    text: str | None = Field(default=None)
    embedding: Any = Field(default=None, sa_column=Column(Vector(EMBEDDING_DIM)))


_engine: Engine | None = None
_schema_ready = False
_metadata_values_cache: dict[str, tuple[float, frozenset[str]]] = {}


def _metadata_cache_fresh(cached_at: float) -> bool:
    """TTL gate: bounds cross-process staleness; 0 keeps the legacy behavior."""
    ttl = get_settings().metadata_cache_ttl_s
    return ttl <= 0 or (time.monotonic() - cached_at) < ttl


_INSERT_BATCH_SIZE = 1000

_UPSERT_SQL = """
INSERT INTO chunk (
    id, doc_id, version_id, fda_document_id, fda_version_id, ordinal, page, section_path,
    normalized_name, dosage_form, route, source_url, psg_type, appl_no, short_name,
    source_family, document_type, locator, text, embedding
) VALUES (
    :id, :doc_id, :version_id, :fda_document_id, :fda_version_id, :ordinal, :page,
    :section_path, :normalized_name, :dosage_form, :route, :source_url, :psg_type,
    :appl_no, :short_name, :source_family, :document_type, :locator, :text,
    CAST(:embedding AS vector)
)
ON CONFLICT (id) DO UPDATE SET
    doc_id = EXCLUDED.doc_id,
    version_id = EXCLUDED.version_id,
    fda_document_id = EXCLUDED.fda_document_id,
    fda_version_id = EXCLUDED.fda_version_id,
    ordinal = EXCLUDED.ordinal,
    page = EXCLUDED.page,
    section_path = EXCLUDED.section_path,
    normalized_name = EXCLUDED.normalized_name,
    dosage_form = EXCLUDED.dosage_form,
    route = EXCLUDED.route,
    source_url = EXCLUDED.source_url,
    psg_type = EXCLUDED.psg_type,
    appl_no = EXCLUDED.appl_no,
    short_name = EXCLUDED.short_name,
    source_family = EXCLUDED.source_family,
    document_type = EXCLUDED.document_type,
    locator = EXCLUDED.locator,
    text = EXCLUDED.text,
    embedding = EXCLUDED.embedding
"""


def get_engine() -> Engine:
    """The engine for the pgvector store: `store/db.py`'s shared pool.

    Since R5 the db engine is always Postgres (there is no other dialect), so
    the old build-my-own-engine fallback is gone -- one pool, one set of K2
    parameters, owned by db.py.
    """
    global _engine
    if _engine is None:
        from regwatch.store import db as db_module

        _engine = db_module.get_engine()
    return _engine


def _ensure_extension(engine: Engine) -> None:
    """Make the `vector` type available, `extensions`-schema style first.

    Lakebase ships pgvector preinstalled in the `extensions` schema, so the
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
    # Self-heal the HNSW index plus the model-declared btree indexes:
    # Table.create with checkfirst=True skips indexes entirely when the table
    # already exists (e.g. created first by store/db.py's bootstrap DDL), and
    # both bootstrap paths must converge on the same index set (K4) regardless
    # of module-initialization order.
    #
    # The HNSW index is built ONLY under the legacy profile. Lakebase's branch
    # is capped at 512 MiB (branch_logical_size_limit_bytes, tier-fixed, not
    # raisable) and measured headroom has been as low as ~21 MiB; the index is
    # 42 MB on disk and reads exclusively from `chunk.embedding`
    # (retrieve/retriever.py:220-276 only queries that column when
    # active_embedding_profile == "legacy"). Once a named profile serves
    # retrieval, `chunk.embedding` is dead weight and rebuilding its index on
    # every boot silently reclaims the 512 MiB budget out from under an
    # operator running scripts/reclaim_lakebase_space.py. Unconditional
    # `CREATE INDEX IF NOT EXISTS` is exactly why a manual `DROP INDEX` kept
    # coming back.
    active_profile_id = (get_settings().active_embedding_profile or "legacy").strip()
    hnsw_ddl = (
        (
            # Legacy name kept deliberately: the 0026 rename is parked until this
            # ships, so the DDL must match what is actually in prod (0025).
            "CREATE INDEX IF NOT EXISTS ix_chunk_embedding_hnsw "
            "ON chunk USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)",
        )
        if active_profile_id == "legacy"
        else ()
    )
    index_ddl = (
        *hnsw_ddl,
        *(
            f"CREATE INDEX IF NOT EXISTS ix_chunk_{column} ON chunk ({column})"
            for column in (
                "normalized_name",
                "doc_id",
                "version_id",
                "fda_document_id",
                "fda_version_id",
                "source_family",
                "document_type",
                "appl_no",
            )
        ),
    )
    # Lock-safe like _enable_chunk_rls below and db.py's _ensure_postgres_objects:
    # CREATE INDEX IF NOT EXISTS takes a ShareLock on the hot `chunk` table even
    # when the index already exists, which conflicts with the RowExclusiveLock a
    # concurrent ingest writer holds (the 2026-06-18 lock-pileup class) -- and
    # this path runs lazily on a booted process's FIRST query/ingest. So each
    # statement gets its own transaction under a short lock_timeout; a contended
    # run is logged and skipped rather than surfacing as an uncaught 500. All
    # statements target the same table, so once one is contended the rest will
    # be too: break instead of paying lock_timeout per index. Every statement is
    # IF NOT EXISTS, so the next boot/first-use re-attempts the skipped ones.
    for ddl in index_ddl:
        try:
            with engine.begin() as conn:
                conn.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
                conn.execute(sa_text(ddl))
        except DBAPIError as exc:
            # Same allowlist as boot (db.ddl_degrade_reason): a contended lock, a
            # role without ownership of `chunk`, or a read-only session are all
            # environment refusals this lazy path must survive -- it runs on a
            # SERVING process's first query, where an escape is a naked
            # unaudited 500. A broken statement of ours still raises.
            reason = ddl_degrade_reason(exc)
            if reason is None:
                raise
            log.warning(
                "ensure_schema_index_skipped",
                reason=reason,
                error=str(getattr(exc, "orig", exc)),
            )
            break
    # Deny-all for any Data API caller; the app's `regwatch_app` role holds
    # BYPASSRLS. Mandatory per the K2 contract — but enabled OUT of the
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
    except DBAPIError as exc:
        # Contended ACCESS EXCLUSIVE lock, no ownership of `chunk`, or a
        # read-only session: never let it wedge the first query. db.py's sweep
        # publishes an unprotected `chunk` to /ready, so this stays detected.
        reason = ddl_degrade_reason(exc)
        if reason is None:
            raise
        log.warning(
            "chunk_rls_enable_skipped",
            reason=reason,
            error=str(getattr(exc, "orig", exc)),
        )


def assert_embedding_provider_dim() -> None:
    """Fail closed on mixed legacy geometry or an unsafe profile promotion."""
    settings = get_settings()
    active_profile_id = (settings.active_embedding_profile or "legacy").strip()
    provider = get_embedding_provider()
    if active_profile_id == "legacy" and isinstance(provider, Qwen3EmbeddingProvider):
        raise RuntimeError(
            "Qwen3 cannot write directly into the unversioned legacy vector space. "
            "Register/backfill a named embedding profile, then set "
            "ACTIVE_EMBEDDING_PROFILE to that profile ID."
        )
    # This check only protects the LEGACY `chunk.embedding` column, whose typmod
    # is vector(1536). It must therefore only fire when that column is what
    # serves retrieval. The exemption used to be `isinstance(provider,
    # Qwen3EmbeddingProvider)`, which encoded "the one non-legacy provider we
    # happen to have" rather than the actual condition, so the OpenAI cutover
    # (2026-08-20) tripped a guard whose own error message told the operator to
    # do exactly what they had already done -- serve through a named profile.
    # Key it on the active profile, which is the real invariant.
    serves_legacy_column = active_profile_id == "legacy"
    if serves_legacy_column and int(provider.dim) != EMBEDDING_DIM:
        raise RuntimeError(
            f"EMBEDDING_PROVIDER={provider.name!r} produces {provider.dim}-dim vectors, "
            f"but the Postgres chunk table stores vector({EMBEDDING_DIM}). "
            "The legacy vector space is retired: serve retrieval through a "
            "named embedding profile (ACTIVE_EMBEDDING_PROFILE) instead."
        )
    if active_profile_id != "legacy":
        from regwatch.store.embedding_profiles import assert_profile_ready_for_activation

        profile = assert_profile_ready_for_activation(active_profile_id)
        get_embedding_provider_for_profile(profile)

    shadow_profile_id = (settings.embedding_shadow_profile or "").strip()
    if shadow_profile_id and shadow_profile_id != active_profile_id:
        from regwatch.store.embedding_profiles import get_embedding_profile

        shadow_profile = get_embedding_profile(shadow_profile_id)
        get_embedding_provider_for_profile(shadow_profile)


def assert_embedding_write_config(profile_id: str) -> Any:
    """Preflight one embedding WRITE target and return its provider.

    The geometry-and-config half of ``assert_embedding_provider_dim``, without
    its coverage half: a backfill must be allowed to start against an
    incomplete profile (completeness is the state it exists to repair), but it
    must never be allowed to start against a provider that cannot possibly
    write the target space. Run this before any document work is spent -- the
    2026-08-14 backfill paid fetch/parse/OCR for 295 documents that all failed
    this exact check at write time.

    Args:
      profile_id: ``legacy`` for the unversioned ``chunk.embedding`` column,
        or a registered embedding profile id.

    Returns:
      The resolved embedding provider for the target space.

    Raises:
      RuntimeError: No provider is configured, the provider cannot write the
        target geometry, or the profile's declared geometry does not match
        the configured endpoint.
    """
    normalized = (profile_id or "").strip()
    if not normalized:
        raise RuntimeError("embedding write preflight requires a profile id")
    if normalized == "legacy":
        provider = get_embedding_provider()
        if isinstance(provider, Qwen3EmbeddingProvider):
            raise RuntimeError(
                "Qwen3 cannot write directly into the unversioned legacy vector "
                "space. Register/backfill a named embedding profile, then set "
                "ACTIVE_EMBEDDING_PROFILE to that profile ID."
            )
        if int(provider.dim) != EMBEDDING_DIM:
            raise RuntimeError(
                f"EMBEDDING_PROVIDER={provider.name!r} produces {provider.dim}-dim "
                f"vectors, but the Postgres chunk table stores vector({EMBEDDING_DIM}); "
                "refusing before any document is fetched."
            )
        return provider
    from regwatch.store.embedding_profiles import get_embedding_profile

    return get_embedding_provider_for_profile(get_embedding_profile(normalized))


def _ensure_ready() -> None:
    global _schema_ready
    if _schema_ready:
        return
    assert_embedding_provider_dim()
    ensure_schema(get_engine())
    _schema_ready = True


def reset_for_tests() -> None:
    """Drop cached engine/schema state so tests can re-point DATABASE_URL.

    The engine reference is db.py's shared pool -- db.reset_for_tests()
    owns disposing it; here we only drop the reference and caches.
    """
    global _engine, _schema_ready
    _engine = None
    _schema_ready = False
    _metadata_values_cache.clear()
    # The additive profile store shares this engine/schema lifecycle but keeps
    # its own lazy-ready bit to avoid import cycles at module load.
    from regwatch.store import embedding_profiles

    embedding_profiles.reset_for_tests()


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
    embeddings: Sequence[list[float] | None],
    documents: list[str],
    metadatas: list[dict[str, Any]],
    *,
    conn: Connection | None = None,
) -> None:
    """Batched chunk upsert.

    With ``conn`` the upserts execute on the CALLER'S connection/transaction
    (the ingest pipeline's atomic version+chunks commit) -- the caller owns
    commit/rollback and this function must not begin/end anything. Without it,
    the historical behavior: one self-contained transaction on this store's
    engine.
    """
    if not ids:
        return
    if not (len(ids) == len(embeddings) == len(documents) == len(metadatas)):
        raise ValueError("ids, embeddings, documents, metadatas must have equal lengths")
    if conn is not None and conn.dialect.name != "postgresql":
        # A non-Postgres session sneaking in here would write chunks to the
        # wrong database entirely; refuse instead of guessing.
        raise ValueError("add_chunks(conn=...) requires a Postgres connection")
    rows: list[dict[str, Any]] = []
    for chunk_id, embedding, document, meta in zip(
        ids, embeddings, documents, metadatas, strict=True
    ):
        if embedding is not None:
            _validate_embedding(embedding)
        meta = meta or {}
        rows.append(
            {
                "id": chunk_id,
                "doc_id": _as_int(meta.get("doc_id")),
                "version_id": _as_int(meta.get("version_id")),
                "fda_document_id": _as_int(meta.get("fda_document_id")),
                "fda_version_id": _as_int(meta.get("fda_version_id")),
                "ordinal": _as_int(meta.get("ordinal")),
                "page": _as_int(meta.get("page")),
                "section_path": _as_text(meta.get("section_path")),
                "normalized_name": _as_text(meta.get("normalized_name")),
                "dosage_form": _as_text(meta.get("dosage_form")),
                "route": _as_text(meta.get("route")),
                "source_url": _as_text(meta.get("source_url")),
                "psg_type": _as_text(meta.get("psg_type")),
                "appl_no": _as_text(meta.get("appl_no")),
                "short_name": _short_name(meta),
                "source_family": _as_text(meta.get("source_family")),
                "document_type": _as_text(meta.get("document_type")),
                "locator": _as_text(meta.get("locator")),
                "text": document,
                "embedding": _vector_literal(embedding) if embedding is not None else None,
            }
        )
    _ensure_ready()
    if conn is not None:
        for start in range(0, len(rows), _INSERT_BATCH_SIZE):
            conn.execute(sa_text(_UPSERT_SQL), rows[start : start + _INSERT_BATCH_SIZE])
    else:
        engine = get_engine()
        with engine.begin() as own_conn:
            for start in range(0, len(rows), _INSERT_BATCH_SIZE):
                own_conn.execute(sa_text(_UPSERT_SQL), rows[start : start + _INSERT_BATCH_SIZE])
    # A caller-owned transaction may still roll back after this returns; an
    # eagerly-cleared cache merely repopulates, so clearing here stays correct.
    _metadata_values_cache.clear()


def update_legacy_chunk_embeddings(
    chunk_ids: list[str],
    documents: list[str],
    embeddings: list[list[float]],
) -> None:
    """Checkpoint legacy vectors without modifying chunk text or provenance.

    ``text = :document`` is an optimistic concurrency guard: if a revision
    replaced a chunk after it was selected for embedding, the stale vector is
    rejected and the new row remains pending for the next pass.
    """

    if not (len(chunk_ids) == len(documents) == len(embeddings)):
        raise ValueError("chunk_ids, documents, and embeddings must have equal lengths")
    if not chunk_ids:
        return
    rows: list[dict[str, str]] = []
    for chunk_id, document, embedding in zip(chunk_ids, documents, embeddings, strict=True):
        _validate_embedding(embedding)
        rows.append(
            {
                "chunk_id": chunk_id,
                "document": document,
                "embedding": _vector_literal(embedding),
            }
        )
    _ensure_ready()
    with get_engine().begin() as conn:
        result = conn.execute(
            sa_text(
                "UPDATE chunk SET embedding = CAST(:embedding AS vector) "
                "WHERE id = :chunk_id AND fda_document_id IS NOT NULL "
                "AND text = :document"
            ),
            rows,
        )
    if int(result.rowcount or 0) != len(rows):
        raise RuntimeError(
            "authoritative chunk changed during legacy embedding backfill; retry the batch"
        )


def delete_chunks_for_doc_except_version(doc_id: int, keep_version_id: int) -> int:
    """Delete indexed chunks for one PSG document except the current version.

    A NULL/unparseable version_id never matches the kept version, so those
    rows are deleted too (`IS DISTINCT FROM` keeps NULLs in the delete set).
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


def delete_chunks_for_doc(doc_id: int, *, conn: Connection) -> int:
    """Delete ALL indexed chunks for one PSG document, on the CALLER'S
    connection/transaction.

    Exists for the re-chunk driver: a chunking-recipe change can produce FEWER
    chunks for a version than the recipe it replaces, and the id-keyed upsert
    alone would leave the old recipe's high-ordinal rows behind as stale
    retrieval hits. Delete-then-insert inside one transaction is the only
    shape that cannot strand them; there is deliberately no own-engine
    fallback here.
    """
    if conn.dialect.name != "postgresql":
        raise ValueError("delete_chunks_for_doc(conn=...) requires a Postgres connection")
    _ensure_ready()
    result = conn.execute(sa_text("DELETE FROM chunk WHERE doc_id = :doc_id"), {"doc_id": doc_id})
    deleted = int(result.rowcount or 0)
    if deleted:
        _metadata_values_cache.clear()
    return deleted


def delete_chunks_for_fda_document(fda_document_id: int, *, conn: Connection) -> int:
    """Delete current-search chunks for one authoritative FDA document.

    The caller must insert the replacement version in the same transaction.
    Historical version rows remain immutable; only the current-search index is
    replaced, so retrieval can never mix revisions of one document.
    """
    if conn.dialect.name != "postgresql":
        raise ValueError("delete_chunks_for_fda_document(conn=...) requires a Postgres connection")
    _ensure_ready()
    result = conn.execute(
        sa_text("DELETE FROM chunk WHERE fda_document_id = :fda_document_id"),
        {"fda_document_id": fda_document_id},
    )
    deleted = int(result.rowcount or 0)
    if deleted:
        _metadata_values_cache.clear()
    return deleted


def _append_condition(
    column: str,
    value: object,
    conditions: list[str],
    params: dict[str, Any],
    *,
    table_alias: str = "",
) -> None:
    if column not in _FILTERABLE_COLUMNS:
        raise ValueError(f"unsupported chunk filter field: {column!r}")
    if table_alias not in {"", "c"}:
        raise ValueError(f"unsupported chunk table alias: {table_alias!r}")
    sql_column = f"{table_alias}.{column}" if table_alias else column

    def coerce(v: object) -> object:
        # Match the bound value to the column's declared type: a client-supplied
        # string version_id/doc_id/page (filters is dict[str, Any] off the wire)
        # would otherwise mismatch the integer column instead of filtering
        # correctly.
        return _as_int(v) if column in _INT_METADATA_COLUMNS else v

    if isinstance(value, dict):
        ops = set(value)
        if ops == {"$eq"}:
            param = f"p{len(params)}"
            conditions.append(f"{sql_column} = :{param}")
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
            conditions.append(f"{sql_column} = ANY(:{param})")
            params[param] = [coerce(x) for x in seq]
            return
        raise ValueError(f"unsupported filter operator(s) {ops!r} for {column!r}")
    param = f"p{len(params)}"
    conditions.append(f"{sql_column} = :{param}")
    params[param] = coerce(value)


def _parse_where(
    node: dict[str, Any],
    conditions: list[str],
    params: dict[str, Any],
    *,
    table_alias: str = "",
) -> None:
    for key, value in node.items():
        if key == "$and":
            if not isinstance(value, list):
                raise ValueError("$and requires a list of clauses")
            for sub in value:
                if not isinstance(sub, dict):
                    raise ValueError("$and clauses must be dicts")
                _parse_where(sub, conditions, params, table_alias=table_alias)
        elif key.startswith("$"):
            raise ValueError(f"unsupported where operator: {key!r}")
        else:
            _append_condition(key, value, conditions, params, table_alias=table_alias)


def _where_clause(
    where: dict[str, Any] | None,
    *,
    table_alias: str = "",
) -> tuple[str, dict[str, Any]]:
    """Compile a `where` filter ({field: {"$eq"/"$in": ...}}, "$and") to SQL."""
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if where:
        _parse_where(where, conditions, params, table_alias=table_alias)
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
    filtered = bool(clause)
    clause = f"{clause} AND embedding IS NOT NULL" if clause else " WHERE embedding IS NOT NULL"
    params["qvec"] = _vector_literal(query_embedding)
    params["k"] = int(k)
    select_cols = ", ".join(("id", "text", *_METADATA_COLUMNS))
    # S608: the only interpolated identifiers are _METADATA_COLUMNS (a module
    # constant) and `clause`, whose column names _append_condition validates
    # against _FILTERABLE_COLUMNS. Every caller value is a bound parameter.
    sql = (
        f"SELECT {select_cols}, embedding <=> CAST(:qvec AS vector) AS distance "  # noqa: S608
        f"FROM chunk{clause} "
        "ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :k"
    )
    with get_engine().begin() as conn:
        if filtered:
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
        # Score convention -- PINNED (store/vector_store.py, similarity_search):
        #   pgvector `<=>` returns cosine distance d = 1 - cos_sim, d in [0, 2]
        #   score = 1 - d/2 in [0, 1]  (1.0 identical, 0.5 orthogonal,
        #   0.0 opposite)
        # REFUSAL_SCORE_THRESHOLD (default 0.30) is calibrated against this
        # scale, so changing it silently moves the refusal boundary.
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
                "SELECT 1 FROM chunk WHERE doc_id = :doc_id AND version_id = :version_id LIMIT 1"
            ),
            {"doc_id": doc_id, "version_id": version_id},
        ).scalar()
    return found is not None


def chunk_texts_at(short_name: str, page: int) -> list[str]:
    """Every chunk's text at one (short_name, page), for gold-set verification.

    A page can hold several chunks (the sliding window splits within a page), so
    this returns all of them and the caller decides whether its quote appears in
    ANY. Returns [] when the pair does not exist at all, which the caller must
    distinguish from "exists but the quote is absent" -- the first means the gold
    set points at a document/page the corpus does not have, the second means the
    page number is wrong.
    """
    _ensure_ready()
    with get_engine().connect() as conn:
        rows = conn.execute(
            sa_text(
                "SELECT text FROM chunk WHERE short_name = :short_name "
                "AND page = :page ORDER BY ordinal"
            ),
            {"short_name": short_name, "page": page},
        ).scalars()
        return [r for r in rows if r]


def document_chunks(doc_id: int, version_id: int) -> list[tuple[int, int, str]]:
    """Every chunk of one document version as (ordinal, page, text).

    The whole document, in stored order, for callers that need to read it
    rather than search it -- the studio's reference renderer. Rows with no
    text are dropped; a NULL ordinal (written before 0017 added the column)
    sorts last on its page rather than taking the document down.
    """
    _ensure_ready()
    with get_engine().connect() as conn:
        rows = conn.execute(
            sa_text(
                "SELECT COALESCE(ordinal, 2147483647) AS ordinal, "
                "COALESCE(page, 1) AS page, text FROM chunk "
                "WHERE doc_id = :doc_id AND version_id = :version_id "
                "ORDER BY page, ordinal"
            ),
            {"doc_id": doc_id, "version_id": version_id},
        ).all()
    return [(int(r[0]), int(r[1]), r[2]) for r in rows if r[2]]


def distinct_metadata_values(key: str) -> set[str]:
    """Distinct non-empty values of one text metadata column.

    Cached with a TTL; `add_chunks` and test resets invalidate the cache.
    Non-text/unknown keys return an empty set -- same as a metadata key
    that no chunk carries.
    """
    cached = _metadata_values_cache.get(key)
    if cached is not None and _metadata_cache_fresh(cached[0]):
        return set(cached[1])
    if key not in _TEXT_METADATA_COLUMNS:
        # Cache the empty result too: every key is cached, real metadata
        # column or not, so lookup behavior stays uniform.
        _metadata_values_cache[key] = (time.monotonic(), frozenset())
        return set()
    _ensure_ready()
    with get_engine().connect() as conn:
        rows = conn.execute(
            # `key` is rejected above unless it is in _TEXT_METADATA_COLUMNS.
            sa_text(
                f"SELECT DISTINCT {key} FROM chunk WHERE {key} IS NOT NULL AND {key} != ''"  # noqa: S608
            )
        ).scalars()
        out = {str(v) for v in rows}
    _metadata_values_cache[key] = (time.monotonic(), frozenset(out))
    return out
