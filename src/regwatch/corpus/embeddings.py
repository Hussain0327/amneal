"""Batched, resumable embedding backfill scoped to authoritative FDA chunks."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text as sa_text

from regwatch.common.logging import get_logger
from regwatch.corpus.lifecycle import (
    mark_embedding_failed,
    refresh_embedding_states_for_chunks,
    version_ids_for_chunks,
)
from regwatch.store.db import get_engine
from regwatch.store.vector_store import (
    assert_embedding_write_config,
    get_embedding_profile,
    update_legacy_chunk_embeddings,
    upsert_profile_embeddings,
)

log = get_logger(__name__)

# Lakebase's logical-size cap is tier-fixed and NOT raisable via the control
# plane API (measured 2026-08-18: branch_logical_size_limit_bytes ==
# 536,870,912 exactly). A batched write that never checks it can exhaust the
# last few MiB mid-run with no earlier warning; PostgreSQL's own OOM-style
# "no space left" error surfaces mid-transaction, not before it starts.
LAKEBASE_LOGICAL_SIZE_LIMIT_BYTES = 536_870_912


@dataclass(frozen=True)
class StorageHeadroom:
    database_size_bytes: int
    limit_bytes: int

    @property
    def free_bytes(self) -> int:
        return max(self.limit_bytes - self.database_size_bytes, 0)

    @property
    def free_mb(self) -> float:
        return self.free_bytes / (1024 * 1024)


class StorageHeadroomError(RuntimeError):
    """Free Lakebase headroom is below a caller's configured floor."""


def storage_headroom() -> StorageHeadroom | None:
    """Read the live database size against the Lakebase tier-fixed cap.

    Returns ``None`` when the size cannot be measured. The cap is a property of
    a Lakebase BRANCH; a disposable local or CI Postgres has no such limit, and
    there `pg_database_size` can simply time out under the suite's tight
    statement_timeout. Crashing there turned an unrelated CLI assertion into an
    OperationalError and told the operator nothing useful, so an unmeasurable
    database is reported as "unknown" and the caller decides.
    """
    try:
        with get_engine().connect() as conn:
            size = conn.execute(sa_text("SELECT pg_database_size(current_database())")).scalar_one()
    except Exception as exc:
        log.warning("storage_headroom_unavailable", error_type=type(exc).__name__)
        return None
    return StorageHeadroom(
        database_size_bytes=int(size), limit_bytes=LAKEBASE_LOGICAL_SIZE_LIMIT_BYTES
    )


def assert_storage_headroom(min_free_mb: float) -> StorageHeadroom:
    """Raise ``StorageHeadroomError`` when free headroom is below ``min_free_mb``.

    Call once before a batched write starts and again every few batches: a
    check that only runs at the top can still let a long run exhaust the cap.
    """
    if min_free_mb < 0:
        raise ValueError("min_free_mb must be non-negative")
    headroom = storage_headroom()
    if headroom is None:
        # Unmeasurable: not a Lakebase branch, or the probe was cancelled. The
        # floor cannot be enforced, and refusing every write on an unmeasurable
        # database would block local and CI runs that have no cap at all.
        return StorageHeadroom(database_size_bytes=0, limit_bytes=LAKEBASE_LOGICAL_SIZE_LIMIT_BYTES)
    if headroom.free_mb < min_free_mb:
        raise StorageHeadroomError(
            f"Lakebase free space {headroom.free_mb:.1f} MiB is below the "
            f"configured floor {min_free_mb:.1f} MiB (database_size="
            f"{headroom.database_size_bytes} bytes, limit={headroom.limit_bytes} bytes)"
        )
    return headroom


@dataclass(frozen=True)
class PendingCorpusChunk:
    chunk_id: str
    text: str
    content_hash: str


@dataclass(frozen=True)
class CorpusEmbeddingCounts:
    profile_id: str
    chunks: int
    embedded_chunks: int

    @property
    def pending_chunks(self) -> int:
        return max(self.chunks - self.embedded_chunks, 0)


def pending_corpus_chunks(
    profile_id: str,
    *,
    limit: int = 128,
    shard_id: int | None = None,
    canonical_ids: list[str] | None = None,
) -> list[PendingCorpusChunk]:
    """Return one deterministic pending page from the FDA namespace only."""

    normalized = profile_id.strip()
    if limit <= 0:
        return []
    if shard_id is not None and not 0 <= shard_id < 512:
        raise ValueError("shard_id must be between 0 and 511")
    if canonical_ids == []:
        return []
    params: dict[str, object] = {"limit": int(limit)}
    scope = ""
    if shard_id is not None:
        scope += " AND d.shard_id = :shard_id"
        params["shard_id"] = shard_id
    if canonical_ids is not None:
        scope += " AND d.canonical_id = ANY(CAST(:canonical_ids AS text[]))"
        params["canonical_ids"] = canonical_ids
    if normalized == "legacy":
        query = sa_text(
            "SELECT c.id, c.text FROM chunk c JOIN fda_document d "  # noqa: S608
            "ON d.id = c.fda_document_id "
            "WHERE c.fda_document_id IS NOT NULL AND c.text IS NOT NULL "
            f"AND c.embedding IS NULL{scope} ORDER BY c.id LIMIT :limit"
        )
    else:
        get_embedding_profile(normalized)
        query = sa_text(
            "SELECT c.id, c.text FROM chunk c JOIN fda_document d "  # noqa: S608
            "ON d.id = c.fda_document_id "
            "LEFT JOIN chunk_embedding ce ON ce.chunk_id = c.id "
            "AND ce.profile_id = :profile_id "
            "WHERE c.fda_document_id IS NOT NULL AND c.text IS NOT NULL "
            f"AND ce.chunk_id IS NULL{scope} ORDER BY c.id LIMIT :limit"
        )
        params["profile_id"] = normalized
    with get_engine().connect() as conn:
        rows = conn.execute(query, params).mappings().all()
    return [
        PendingCorpusChunk(
            chunk_id=str(row["id"]),
            text=str(row["text"]),
            content_hash=hashlib.sha256(str(row["text"]).encode("utf-8")).hexdigest(),
        )
        for row in rows
    ]


def write_corpus_embeddings(
    profile_id: str,
    chunks: list[PendingCorpusChunk],
    embeddings: list[list[float]],
) -> None:
    """Persist one batch as its durable checkpoint."""

    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must have equal lengths")
    normalized = profile_id.strip()
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    version_ids = version_ids_for_chunks(chunk_ids)
    try:
        if normalized == "legacy":
            update_legacy_chunk_embeddings(
                chunk_ids,
                [chunk.text for chunk in chunks],
                embeddings,
            )
        else:
            upsert_profile_embeddings(
                normalized,
                chunk_ids,
                embeddings,
                [chunk.content_hash for chunk in chunks],
            )
    except Exception as exc:
        for version_id in version_ids:
            mark_embedding_failed(version_id, normalized, exc)
        raise
    refresh_embedding_states_for_chunks(normalized, chunk_ids)


def corpus_embedding_counts(profile_id: str) -> CorpusEmbeddingCounts:
    normalized = profile_id.strip()
    params: dict[str, object] = {}
    if normalized == "legacy":
        query = sa_text(
            "SELECT count(*) AS chunks, count(c.embedding) AS embedded "
            "FROM chunk c WHERE c.fda_document_id IS NOT NULL"
        )
    else:
        get_embedding_profile(normalized)
        query = sa_text(
            "SELECT count(*) AS chunks, count(ce.chunk_id) AS embedded "
            "FROM chunk c "
            "LEFT JOIN chunk_embedding ce ON ce.chunk_id = c.id "
            "AND ce.profile_id = :profile_id "
            "WHERE c.fda_document_id IS NOT NULL"
        )
        params["profile_id"] = normalized
    with get_engine().connect() as conn:
        row = conn.execute(query, params).one()
    return CorpusEmbeddingCounts(
        profile_id=normalized,
        chunks=int(row[0]),
        embedded_chunks=int(row[1]),
    )


def embed_pending_corpus(
    profile_id: str,
    *,
    batch_size: int = 128,
    limit: int = 0,
    shard_id: int | None = None,
    canonical_ids: list[str] | None = None,
    on_batch: Callable[[int], None] | None = None,
) -> int:
    """Embed a resumable scope and return the number of chunks written."""

    from regwatch.process.embedder import embed_documents

    normalized = profile_id.strip()
    if not normalized:
        raise ValueError("profile_id must not be blank")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if limit < 0:
        raise ValueError("limit must be non-negative")
    # Geometry preflight before the first pending page is read: a provider
    # that cannot write the target space refuses here, once, not per-batch.
    provider = assert_embedding_write_config(normalized)

    processed = 0
    while limit == 0 or processed < limit:
        page_size = batch_size if limit == 0 else min(batch_size, limit - processed)
        pending = pending_corpus_chunks(
            normalized,
            limit=page_size,
            shard_id=shard_id,
            canonical_ids=canonical_ids,
        )
        if not pending:
            break
        vectors = embed_documents(provider, [chunk.text for chunk in pending])
        write_corpus_embeddings(normalized, pending, vectors)
        processed += len(pending)
        if on_batch is not None:
            on_batch(processed)
    return processed
