"""Immutable embedding profiles and additive, profile-keyed chunk vectors.

The legacy production path remains ``chunk.embedding vector(1536)``.  This
module adds a parallel storage seam for evaluating and backfilling a candidate
embedding model without mixing vector spaces or changing active retrieval.

``chunk_embedding.embedding`` intentionally has no dimension typmod.  Each row
references an immutable ``embedding_profile`` whose dimension is enforced by a
database trigger.  That lets multiple profiles coexist while per-profile
expression indexes cast to:

* ``vector(d)`` for dimensions <= 2,000; or
* ``halfvec(d)`` for dimensions 2,001..4,000.

The latter keeps a full-precision ``vector`` value for reranking while making
Qwen3-Embedding-4B's native 2,560 dimensions indexable with pgvector HNSW.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import Engine, ForeignKey, Index, Table
from sqlalchemy import text as sa_text
from sqlmodel import SQLModel

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from regwatch.store.vector_store import Hit


_PROFILE_ID_RE = re.compile(r"^ep_[0-9a-f]{32}$")
_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_TEXT_FIELDS = (
    "provider",
    "model",
    "revision",
    "dtype",
    "normalization",
    "query_instruction_version",
    "preprocessing_version",
    "chunking_version",
    "serving_runtime_version",
)
_VECTOR_HNSW_MAX_DIM = 2000
_HALFVEC_HNSW_MAX_DIM = 4000
_WRITE_BATCH_SIZE = 1000


@dataclass(frozen=True)
class EmbeddingProfileSpec:
    """All inputs that define one immutable embedding geometry."""

    provider: str
    model: str
    revision: str
    dimension: int
    dtype: str
    normalization: str
    query_instruction_version: str
    preprocessing_version: str
    chunking_version: str
    serving_runtime_version: str

    def __post_init__(self) -> None:
        for field_name in _PROFILE_TEXT_FIELDS:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"embedding profile {field_name} must be a non-empty string")
        if isinstance(self.dimension, bool) or not isinstance(self.dimension, int):
            raise ValueError("embedding profile dimension must be an integer")
        if not 1 <= self.dimension <= 16000:
            raise ValueError("embedding profile dimension must be between 1 and 16000")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def profile_id(self) -> str:
        return f"ep_{self.fingerprint[:32]}"


@dataclass(frozen=True)
class EmbeddingProfile:
    profile_id: str
    fingerprint: str
    provider: str
    model: str
    revision: str
    dimension: int
    dtype: str
    normalization: str
    query_instruction_version: str
    preprocessing_version: str
    chunking_version: str
    serving_runtime_version: str
    created_at: datetime

    @property
    def spec(self) -> EmbeddingProfileSpec:
        return EmbeddingProfileSpec(
            provider=self.provider,
            model=self.model,
            revision=self.revision,
            dimension=self.dimension,
            dtype=self.dtype,
            normalization=self.normalization,
            query_instruction_version=self.query_instruction_version,
            preprocessing_version=self.preprocessing_version,
            chunking_version=self.chunking_version,
            serving_runtime_version=self.serving_runtime_version,
        )


@dataclass(frozen=True)
class PendingProfileChunk:
    chunk_id: str
    text: str
    content_hash: str


@dataclass(frozen=True)
class ProfileEmbeddingCoverage:
    profile_id: str
    total_chunks: int
    embedded_chunks: int

    @property
    def pending_chunks(self) -> int:
        return max(0, self.total_chunks - self.embedded_chunks)

    @property
    def complete(self) -> bool:
        return self.total_chunks == self.embedded_chunks


@dataclass(frozen=True)
class ProfileIndexSpec:
    profile_id: str
    index_name: str
    dimension: int
    index_dtype: str
    concurrently: bool

    @property
    def uses_halfvec(self) -> bool:
        return self.index_dtype == "halfvec"


# Core tables instead of ORM rows: callers use immutable value dataclasses and
# explicit SQL, while registering these tables in SQLModel.metadata keeps the
# fresh-Postgres create_all + stamp-head path converged with migration replay.
embedding_profile_table = Table(
    "embedding_profile",
    SQLModel.metadata,
    sa.Column("profile_id", sa.String(), primary_key=True),
    sa.Column("fingerprint", sa.String(64), nullable=False),
    sa.Column("provider", sa.String(), nullable=False),
    sa.Column("model", sa.String(), nullable=False),
    sa.Column("revision", sa.String(), nullable=False),
    sa.Column("dimension", sa.Integer(), nullable=False),
    sa.Column("dtype", sa.String(), nullable=False),
    sa.Column("normalization", sa.String(), nullable=False),
    sa.Column("query_instruction_version", sa.String(), nullable=False),
    sa.Column("preprocessing_version", sa.String(), nullable=False),
    sa.Column("chunking_version", sa.String(), nullable=False),
    sa.Column("serving_runtime_version", sa.String(), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "profile_id ~ '^ep_[0-9a-f]{32}$'",
        name="ck_embedding_profile_id",
    ).ddl_if(dialect="postgresql"),
    sa.CheckConstraint(
        "fingerprint ~ '^[0-9a-f]{64}$'",
        name="ck_embedding_profile_fingerprint",
    ).ddl_if(dialect="postgresql"),
    sa.CheckConstraint(
        "dimension BETWEEN 1 AND 16000",
        name="ck_embedding_profile_dimension",
    ),
    sa.UniqueConstraint(
        "fingerprint",
        name="uq_embedding_profile_fingerprint",
    ),
)

chunk_embedding_table = Table(
    "chunk_embedding",
    SQLModel.metadata,
    sa.Column(
        "profile_id",
        sa.String(),
        ForeignKey("embedding_profile.profile_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column(
        "chunk_id",
        sa.String(),
        ForeignKey("chunk.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # No typmod: the profile dimension trigger is the source of truth.
    sa.Column("embedding", Vector(), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column(
        "embedded_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "content_hash ~ '^[0-9a-f]{64}$'",
        name="ck_chunk_embedding_content_hash",
    ).ddl_if(dialect="postgresql"),
)
Index("ix_chunk_embedding_chunk_id", chunk_embedding_table.c.chunk_id)


_schema_ready = False
_schema_lock = Lock()


def reset_for_tests() -> None:
    global _schema_ready
    _schema_ready = False


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _engine() -> Engine:
    from regwatch.store import db

    return db.get_engine()


def _ensure_ready() -> None:
    """Create only missing additive objects; never touch active profile state."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        from regwatch.store import db, pgvector_store

        engine = _engine()
        pgvector_store.ensure_schema(engine)
        embedding_profile_table.create(engine, checkfirst=True)
        chunk_embedding_table.create(engine, checkfirst=True)
        db._ensure_embedding_profile_objects(engine)
        # A fresh create_all happens before the 0011 event trigger is installed;
        # the regular bootstrap sweep covers that path.  This lazy/self-heal
        # path must converge too, without relying on a caller to remember RLS.
        db._enable_row_level_security(engine)
        _schema_ready = True


def _validate_profile_id(profile_id: str) -> None:
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise ValueError("profile_id must match 'ep_' followed by 32 lowercase hex characters")


def _profile_from_row(row: Any) -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id=str(row["profile_id"]),
        fingerprint=str(row["fingerprint"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        revision=str(row["revision"]),
        dimension=int(row["dimension"]),
        dtype=str(row["dtype"]),
        normalization=str(row["normalization"]),
        query_instruction_version=str(row["query_instruction_version"]),
        preprocessing_version=str(row["preprocessing_version"]),
        chunking_version=str(row["chunking_version"]),
        serving_runtime_version=str(row["serving_runtime_version"]),
        created_at=row["created_at"],
    )


def register_embedding_profile(spec: EmbeddingProfileSpec) -> EmbeddingProfile:
    """Idempotently register an immutable profile derived from its fingerprint."""
    if not isinstance(spec, EmbeddingProfileSpec):
        raise TypeError("spec must be an EmbeddingProfileSpec")
    _ensure_ready()
    params = {
        "profile_id": spec.profile_id,
        "fingerprint": spec.fingerprint,
        **asdict(spec),
    }
    columns = (
        "profile_id",
        "fingerprint",
        "provider",
        "model",
        "revision",
        "dimension",
        "dtype",
        "normalization",
        "query_instruction_version",
        "preprocessing_version",
        "chunking_version",
        "serving_runtime_version",
    )
    insert_sql = (
        f"INSERT INTO embedding_profile ({', '.join(columns)}) "
        f"VALUES ({', '.join(f':{column}' for column in columns)}) "
        "ON CONFLICT (profile_id) DO NOTHING"
    )
    with _engine().begin() as conn:
        conn.execute(sa_text(insert_sql), params)
        row = (
            conn.execute(
                sa_text(
                    "SELECT profile_id, fingerprint, provider, model, revision, dimension, "
                    "dtype, normalization, query_instruction_version, preprocessing_version, "
                    "chunking_version, serving_runtime_version, created_at "
                    "FROM embedding_profile WHERE profile_id = :profile_id"
                ),
                {"profile_id": spec.profile_id},
            )
            .mappings()
            .one()
        )
    profile = _profile_from_row(row)
    if profile.fingerprint != spec.fingerprint or profile.spec != spec:
        # Cryptographic collision or externally-corrupted row: never silently
        # reuse an ID for a different vector geometry.
        raise RuntimeError(f"embedding profile ID collision for {spec.profile_id}")
    return profile


def get_embedding_profile(profile_id: str) -> EmbeddingProfile:
    _validate_profile_id(profile_id)
    _ensure_ready()
    with _engine().connect() as conn:
        row = (
            conn.execute(
                sa_text(
                    "SELECT profile_id, fingerprint, provider, model, revision, dimension, "
                    "dtype, normalization, query_instruction_version, preprocessing_version, "
                    "chunking_version, serving_runtime_version, created_at "
                    "FROM embedding_profile WHERE profile_id = :profile_id"
                ),
                {"profile_id": profile_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise KeyError(f"unknown embedding profile: {profile_id}")
    return _profile_from_row(row)


def list_embedding_profiles() -> list[EmbeddingProfile]:
    _ensure_ready()
    with _engine().connect() as conn:
        rows = (
            conn.execute(
                sa_text(
                    "SELECT profile_id, fingerprint, provider, model, revision, dimension, "
                    "dtype, normalization, query_instruction_version, preprocessing_version, "
                    "chunking_version, serving_runtime_version, created_at "
                    "FROM embedding_profile ORDER BY created_at, profile_id"
                )
            )
            .mappings()
            .all()
        )
    return [_profile_from_row(row) for row in rows]


def pending_profile_chunks(
    profile_id: str,
    *,
    limit: int = 256,
    after_chunk_id: str | None = None,
) -> list[PendingProfileChunk]:
    """Return a deterministic page of chunks not yet embedded for one profile.

    Completed rows are the durable checkpoint.  A rerun can start from the
    beginning and skip completed work, or preserve ``after_chunk_id`` while a
    process is healthy.  A database trigger deletes all profile embeddings when
    ``chunk.text`` changes, so stale rows automatically become pending again.
    """
    _validate_profile_id(profile_id)
    if limit <= 0:
        return []
    get_embedding_profile(profile_id)  # fail before scanning for a typo
    params: dict[str, Any] = {"profile_id": profile_id, "limit": int(limit)}
    cursor_clause = ""
    if after_chunk_id is not None:
        cursor_clause = "AND c.id > :after_chunk_id "
        params["after_chunk_id"] = after_chunk_id
    with _engine().connect() as conn:
        rows = (
            conn.execute(
                sa_text(
                    "SELECT c.id, c.text FROM chunk c "
                    "LEFT JOIN chunk_embedding ce "
                    "ON ce.chunk_id = c.id AND ce.profile_id = :profile_id "
                    "WHERE ce.chunk_id IS NULL AND c.text IS NOT NULL "
                    f"{cursor_clause}"
                    "ORDER BY c.id LIMIT :limit"
                ),
                params,
            )
            .mappings()
            .all()
        )
    return [
        PendingProfileChunk(
            chunk_id=str(row["id"]),
            text=str(row["text"]),
            content_hash=content_hash(str(row["text"])),
        )
        for row in rows
    ]


def _validate_embedding(embedding: list[float], dimension: int) -> None:
    if len(embedding) != dimension:
        raise ValueError(f"embedding has {len(embedding)} dims; profile requires {dimension}")
    if not all(math.isfinite(value) for value in embedding):
        raise ValueError("embedding contains non-finite (NaN/Inf) components")


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in embedding) + "]"


def _upsert_profile_embeddings_on_connection(
    conn: Connection,
    *,
    profile: EmbeddingProfile,
    chunk_ids: list[str],
    embeddings: list[list[float]],
    content_hashes: list[str],
) -> None:
    if conn.dialect.name != "postgresql":
        raise ValueError("profile embeddings require a Postgres connection")
    # Lock source rows against text changes between hash verification and the
    # embedding upsert.  The remote model call happens before this function, so
    # these locks are held only for the short write transaction.
    source_rows = (
        conn.execute(
            sa_text("SELECT id, text FROM chunk WHERE id = ANY(:chunk_ids) FOR SHARE"),
            {"chunk_ids": chunk_ids},
        )
        .mappings()
        .all()
    )
    source_text = {str(row["id"]): str(row["text"] or "") for row in source_rows}
    missing = sorted(set(chunk_ids) - set(source_text))
    if missing:
        raise KeyError(f"unknown chunk IDs: {missing!r}")

    rows: list[dict[str, Any]] = []
    for chunk_id, embedding, expected_hash in zip(
        chunk_ids, embeddings, content_hashes, strict=True
    ):
        _validate_embedding(embedding, profile.dimension)
        if not _CONTENT_HASH_RE.fullmatch(expected_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        actual_hash = content_hash(source_text[chunk_id])
        if expected_hash != actual_hash:
            raise ValueError(
                f"content hash mismatch for chunk {chunk_id!r}; refetch the pending batch"
            )
        rows.append(
            {
                "profile_id": profile.profile_id,
                "chunk_id": chunk_id,
                "embedding": _vector_literal(embedding),
                "content_hash": expected_hash,
            }
        )

    sql = sa_text(
        "INSERT INTO chunk_embedding "
        "(profile_id, chunk_id, embedding, content_hash, embedded_at) "
        "VALUES (:profile_id, :chunk_id, CAST(:embedding AS vector), :content_hash, now()) "
        "ON CONFLICT (profile_id, chunk_id) DO UPDATE SET "
        "embedding = EXCLUDED.embedding, "
        "content_hash = EXCLUDED.content_hash, "
        "embedded_at = EXCLUDED.embedded_at"
    )
    for start in range(0, len(rows), _WRITE_BATCH_SIZE):
        conn.execute(sql, rows[start : start + _WRITE_BATCH_SIZE])


def upsert_profile_embeddings(
    profile_id: str,
    chunk_ids: list[str],
    embeddings: list[list[float]],
    content_hashes: list[str],
    *,
    conn: Connection | None = None,
) -> None:
    """Write one profile only; source hashes prevent stale remote results."""
    _validate_profile_id(profile_id)
    if not chunk_ids:
        return
    if not (len(chunk_ids) == len(embeddings) == len(content_hashes)):
        raise ValueError("chunk_ids, embeddings, and content_hashes must have equal lengths")
    if len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("chunk_ids must be unique within one profile embedding batch")
    _ensure_ready()
    profile = get_embedding_profile(profile_id)
    if conn is not None:
        _upsert_profile_embeddings_on_connection(
            conn,
            profile=profile,
            chunk_ids=chunk_ids,
            embeddings=embeddings,
            content_hashes=content_hashes,
        )
        return
    with _engine().begin() as own_conn:
        _upsert_profile_embeddings_on_connection(
            own_conn,
            profile=profile,
            chunk_ids=chunk_ids,
            embeddings=embeddings,
            content_hashes=content_hashes,
        )


def profile_embedding_coverage(profile_id: str) -> ProfileEmbeddingCoverage:
    _validate_profile_id(profile_id)
    get_embedding_profile(profile_id)
    with _engine().connect() as conn:
        row = (
            conn.execute(
                sa_text(
                    "SELECT "
                    "(SELECT count(*) FROM chunk WHERE text IS NOT NULL) AS total_chunks, "
                    "(SELECT count(*) FROM chunk_embedding "
                    " WHERE profile_id = :profile_id) AS embedded_chunks"
                ),
                {"profile_id": profile_id},
            )
            .mappings()
            .one()
        )
    return ProfileEmbeddingCoverage(
        profile_id=profile_id,
        total_chunks=int(row["total_chunks"]),
        embedded_chunks=int(row["embedded_chunks"]),
    )


def _vector_extension_schema(engine: Engine) -> str:
    with engine.connect() as conn:
        schema = conn.execute(
            sa_text(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname = 'vector'"
            )
        ).scalar()
    if schema not in {"public", "extensions"}:
        raise RuntimeError(f"unsupported pgvector extension schema: {schema!r}")
    return str(schema)


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if numbers is None:
        return ()
    return tuple(int(part or 0) for part in numbers.groups())


def _index_spec(profile: EmbeddingProfile, *, concurrently: bool) -> ProfileIndexSpec:
    if profile.dimension <= _VECTOR_HNSW_MAX_DIM:
        index_dtype = "vector"
    elif profile.dimension <= _HALFVEC_HNSW_MAX_DIM:
        index_dtype = "halfvec"
    else:
        raise ValueError(
            f"profile dimension {profile.dimension} exceeds pgvector HNSW's "
            f"halfvec limit of {_HALFVEC_HNSW_MAX_DIM}"
        )
    return ProfileIndexSpec(
        profile_id=profile.profile_id,
        index_name=f"ix_chunk_embedding_{profile.profile_id[3:19]}_hnsw",
        dimension=profile.dimension,
        index_dtype=index_dtype,
        concurrently=concurrently,
    )


def ensure_profile_hnsw_index(
    profile_id: str,
    *,
    concurrently: bool = True,
) -> ProfileIndexSpec:
    """Create a partial HNSW index scoped to exactly one profile.

    No profile index is built by migrations or boot.  Operators invoke this
    only after a backfill, keeping the additive migration fast and lock-safe.
    """
    profile = get_embedding_profile(profile_id)
    spec = _index_spec(profile, concurrently=concurrently)
    engine = _engine()
    schema = _vector_extension_schema(engine)
    if spec.uses_halfvec:
        with engine.connect() as conn:
            extversion = str(
                conn.execute(
                    sa_text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                ).scalar()
                or ""
            )
        if _version_tuple(extversion) < (0, 7, 0):
            raise RuntimeError(
                f"pgvector {extversion or 'unknown'} lacks halfvec; "
                "version 0.7.0+ is required for a 2560-dimension HNSW index"
            )

    # All interpolated values are either generated from a strict hex profile ID,
    # selected from a two-value constant set, or validated integers.
    qualified_type = f'"{schema}".{spec.index_dtype}'
    qualified_opclass = f'"{schema}".{spec.index_dtype}_cosine_ops'
    concurrently_sql = " CONCURRENTLY" if concurrently else ""
    ddl = (
        f"CREATE INDEX{concurrently_sql} IF NOT EXISTS {spec.index_name} "
        "ON chunk_embedding USING hnsw "
        f"((embedding::{qualified_type}({spec.dimension})) {qualified_opclass}) "
        "WITH (m = 16, ef_construction = 64) "
        f"WHERE profile_id = '{spec.profile_id}'"
    )
    if concurrently:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(sa_text(ddl))
    else:
        with engine.begin() as conn:
            conn.execute(sa_text(ddl))
    return spec


def profile_hnsw_index_ready(profile_id: str) -> bool:
    """Return whether the deterministic profile index is usable by planners."""
    profile = get_embedding_profile(profile_id)
    spec = _index_spec(profile, concurrently=False)
    with _engine().connect() as conn:
        ready = conn.execute(
            sa_text(
                "SELECT i.indisready AND i.indisvalid "
                "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.oid = to_regclass(:index_name)"
            ),
            {"index_name": spec.index_name},
        ).scalar()
    return bool(ready)


def assert_profile_ready_for_activation(profile_id: str) -> EmbeddingProfile:
    """Fail closed unless a profile is complete and has a valid HNSW index."""
    profile = get_embedding_profile(profile_id)
    coverage = profile_embedding_coverage(profile_id)
    if not coverage.complete:
        raise RuntimeError(
            f"embedding profile {profile_id} is incomplete: "
            f"{coverage.embedded_chunks}/{coverage.total_chunks} chunks embedded"
        )
    if not profile_hnsw_index_ready(profile_id):
        raise RuntimeError(
            f"embedding profile {profile_id} has no ready HNSW index; run "
            f"`regwatch embedding-profile-index {profile_id}` before activation"
        )
    return profile


def _distance_expression(*, schema: str, dtype: str, dimension: int, alias: str) -> str:
    qualified_type = f'"{schema}".{dtype}'
    return (
        f"{alias}.embedding::{qualified_type}({dimension}) "
        f"<=> CAST(:qvec AS {qualified_type}({dimension}))"
    )


def similarity_search_profile(
    profile_id: str,
    query_embedding: list[float],
    *,
    k: int = 8,
    where: dict[str, Any] | None = None,
) -> list[Hit]:
    """Search exactly one immutable profile; rows from others cannot participate."""
    from regwatch.store import pgvector_store
    from regwatch.store.vector_store import Hit

    if k <= 0:
        return []
    profile = get_embedding_profile(profile_id)
    _validate_embedding(query_embedding, profile.dimension)
    spec = _index_spec(profile, concurrently=False)
    engine = _engine()
    schema = _vector_extension_schema(engine)
    clause, params = pgvector_store._where_clause(where, table_alias="c")
    params.update(
        {
            "qvec": _vector_literal(query_embedding),
            "k": int(k),
        }
    )
    # The profile ID has already passed a strict lowercase-hex validator.
    # Keeping it literal lets Postgres prove that this query implies the
    # profile-specific partial-index predicate even after psycopg switches to a
    # generic prepared plan.
    profile_predicate = f"ce.profile_id = '{profile_id}'"
    select_cols = ", ".join(
        f"c.{column}" for column in ("id", "text", *pgvector_store._METADATA_COLUMNS)
    )
    full_distance = _distance_expression(
        schema=schema,
        dtype="vector",
        dimension=profile.dimension,
        alias="ce",
    )

    if clause:
        # Exact search over a metadata-narrowed set preserves the existing
        # cross-drug/current-version safety behavior.
        sql = (
            f"SELECT {select_cols}, {full_distance} AS distance "
            "FROM chunk_embedding ce JOIN chunk c ON c.id = ce.chunk_id "
            f"WHERE {profile_predicate} AND {clause.removeprefix(' WHERE ')} "
            f"ORDER BY {full_distance} LIMIT :k"
        )
        with engine.begin() as conn:
            conn.execute(sa_text("SET LOCAL enable_indexscan = off"))
            rows = conn.execute(sa_text(sql), params).mappings().all()
    elif spec.uses_halfvec:
        # HNSW candidates use the half-precision expression index; final score
        # and rank use the stored full-precision vector.
        candidate_distance = _distance_expression(
            schema=schema,
            dtype="halfvec",
            dimension=profile.dimension,
            alias="ce",
        )
        params["candidate_k"] = max(int(k) * 4, 50)
        sql = (
            "WITH candidates AS MATERIALIZED ("
            "SELECT ce.chunk_id FROM chunk_embedding ce "
            f"WHERE {profile_predicate} "
            f"ORDER BY {candidate_distance} LIMIT :candidate_k"
            ") "
            f"SELECT {select_cols}, {full_distance} AS distance "
            "FROM candidates candidate "
            "JOIN chunk_embedding ce "
            f"ON {profile_predicate} AND ce.chunk_id = candidate.chunk_id "
            "JOIN chunk c ON c.id = ce.chunk_id "
            f"ORDER BY {full_distance} LIMIT :k"
        )
        with engine.begin() as conn:
            conn.execute(sa_text("SET LOCAL hnsw.ef_search = 100"))
            rows = conn.execute(sa_text(sql), params).mappings().all()
    else:
        distance = _distance_expression(
            schema=schema,
            dtype="vector",
            dimension=profile.dimension,
            alias="ce",
        )
        sql = (
            f"SELECT {select_cols}, {distance} AS distance "
            "FROM chunk_embedding ce JOIN chunk c ON c.id = ce.chunk_id "
            f"WHERE {profile_predicate} "
            f"ORDER BY {distance} LIMIT :k"
        )
        with engine.begin() as conn:
            conn.execute(sa_text("SET LOCAL hnsw.ef_search = 100"))
            rows = conn.execute(sa_text(sql), params).mappings().all()

    hits: list[Hit] = []
    for row in rows:
        score = max(0.0, min(1.0, 1.0 - float(row["distance"]) / 2.0))
        metadata = {
            column: row[column]
            for column in pgvector_store._METADATA_COLUMNS
            if row[column] is not None
        }
        hits.append(
            Hit(
                chunk_id=str(row["id"]),
                text=str(row["text"] or ""),
                metadata=metadata,
                score=score,
            )
        )
    return hits
