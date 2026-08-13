"""Vector store facade over pgvector (the only backend since R5).

Chunks live in the ``chunk`` table (store/pgvector_store.py). Each chunk's
metadata carries enough to build a citation (`doc_id`, `version_id`, `page`,
`source_url`) and enough to filter by drug (`normalized_name`, `dosage_form`,
`route`). ``Hit.score`` is cosine similarity in [0, 1], computed as
``1 - cosine_distance / 2`` — the scale the refusal threshold is calibrated
against.

This module stays as the seam every caller imports (retriever, ingest
pipeline, watch, API health, resolver) — the Chroma half of the old dual-mode
dispatch is gone, but keeping the facade means callers never changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from regwatch.retrieve.mode import RetrievalMode

if TYPE_CHECKING:  # typing-only import to avoid a cycle with pgvector_store
    from sqlalchemy.engine import Connection

    from regwatch.store.embedding_profiles import (
        EmbeddingProfile,
        EmbeddingProfileSpec,
        PendingProfileChunk,
        ProfileEmbeddingCoverage,
        ProfileIndexSpec,
    )


@dataclass
class Hit:
    chunk_id: str
    text: str
    metadata: dict[str, Any]
    score: float  # cosine similarity in [0, 1] (we convert from distance)


def reset_for_tests() -> None:
    from regwatch.store import pgvector_store

    pgvector_store.reset_for_tests()


def add_chunks(
    ids: list[str],
    embeddings: Sequence[list[float] | None],
    documents: list[str],
    metadatas: list[dict[str, Any]],
    *,
    conn: Connection | None = None,
) -> None:
    """Upsert chunks. ``conn`` joins the caller's open transaction so version
    row and chunks commit or roll back together (the ingest pipeline's atomic
    revision commit); the caller owns commit/rollback.
    """
    if not ids:
        return
    from regwatch.store import pgvector_store

    pgvector_store.add_chunks(ids, embeddings, documents, metadatas, conn=conn)


def update_legacy_chunk_embeddings(
    chunk_ids: list[str],
    documents: list[str],
    embeddings: list[list[float]],
) -> None:
    """Write only legacy vectors for authoritative chunks."""

    from regwatch.store import pgvector_store

    pgvector_store.update_legacy_chunk_embeddings(chunk_ids, documents, embeddings)


def delete_chunks_for_doc_except_version(doc_id: int, keep_version_id: int) -> int:
    """Delete indexed chunks for one PSG document except the current version.

    Old chunks for the same PSG document must not remain retrievable after a
    revision — the chunk table is the current-answer search index; psg_version
    stays the audit store.
    """
    from regwatch.store import pgvector_store

    return pgvector_store.delete_chunks_for_doc_except_version(doc_id, keep_version_id)


def delete_chunks_for_doc(doc_id: int, *, conn: Connection) -> int:
    """Delete ALL chunks for one PSG document on the caller's transaction.

    For the re-chunk driver only: delete-then-insert in one transaction is the
    shape that cannot strand stale high-ordinal rows when a recipe change
    produces fewer chunks (the id-keyed upsert alone would).
    """
    from regwatch.store import pgvector_store

    return pgvector_store.delete_chunks_for_doc(doc_id, conn=conn)


def delete_chunks_for_fda_document(fda_document_id: int, *, conn: Connection) -> int:
    """Replace one authoritative document's current-search rows atomically."""
    from regwatch.store import pgvector_store

    return pgvector_store.delete_chunks_for_fda_document(fda_document_id, conn=conn)


def similarity_search(
    query_embedding: list[float],
    *,
    k: int = 8,
    where: dict[str, Any] | None = None,
) -> list[Hit]:
    from regwatch.store import pgvector_store

    return pgvector_store.similarity_search(query_embedding, k=k, where=where)


def collection_size() -> int:
    from regwatch.store import pgvector_store

    return pgvector_store.collection_size()


def chunks_exist(doc_id: int, version_id: int) -> bool:
    """True iff the search index holds at least one chunk for (doc_id, version_id).

    Used by the ingest pipeline to detect a version row that committed but whose
    chunks never landed, so retrieval is never silently blind to a drug whose
    hash already matches.
    """
    from regwatch.store import pgvector_store

    return pgvector_store.chunks_exist(doc_id, version_id)


def chunk_texts_at(short_name: str, page: int) -> list[str]:
    """Every chunk's text at one (short_name, page). Used to verify gold-set pins."""
    from regwatch.store import pgvector_store

    return pgvector_store.chunk_texts_at(short_name, page)


def document_chunks(doc_id: int, version_id: int) -> list[tuple[int, int, str]]:
    """Every chunk of one document version as (ordinal, page, text).

    The read side of the same rows `similarity_search` ranks: used to render a
    stored document back as prose instead of retrieving passages from it.
    """
    from regwatch.store import pgvector_store

    return pgvector_store.document_chunks(doc_id, version_id)


def distinct_metadata_values(key: str) -> set[str]:
    """All distinct non-empty string values of one metadata `key` across chunks.

    Used by the product resolver to learn which drugs the corpus can answer
    about (pgvector_store caches this with a TTL; `add_chunks` and test resets
    invalidate it).
    """
    from regwatch.store import pgvector_store

    return pgvector_store.distinct_metadata_values(key)


# Additive embedding-profile seam.  None of these functions is used by the
# legacy active path above; callers must name one immutable profile explicitly,
# which prevents accidental cross-space writes or retrieval.
def register_embedding_profile(spec: EmbeddingProfileSpec) -> EmbeddingProfile:
    from regwatch.store import embedding_profiles

    return embedding_profiles.register_embedding_profile(spec)


def get_embedding_profile(profile_id: str) -> EmbeddingProfile:
    from regwatch.store import embedding_profiles

    return embedding_profiles.get_embedding_profile(profile_id)


def list_embedding_profiles() -> list[EmbeddingProfile]:
    from regwatch.store import embedding_profiles

    return embedding_profiles.list_embedding_profiles()


def pending_profile_chunks(
    profile_id: str,
    *,
    limit: int = 256,
    after_chunk_id: str | None = None,
) -> list[PendingProfileChunk]:
    from regwatch.store import embedding_profiles

    return embedding_profiles.pending_profile_chunks(
        profile_id,
        limit=limit,
        after_chunk_id=after_chunk_id,
    )


def upsert_profile_embeddings(
    profile_id: str,
    chunk_ids: list[str],
    embeddings: list[list[float]],
    content_hashes: list[str],
    *,
    conn: Connection | None = None,
) -> None:
    from regwatch.store import embedding_profiles

    embedding_profiles.upsert_profile_embeddings(
        profile_id,
        chunk_ids,
        embeddings,
        content_hashes,
        conn=conn,
    )


def profile_embedding_coverage(profile_id: str) -> ProfileEmbeddingCoverage:
    from regwatch.store import embedding_profiles

    return embedding_profiles.profile_embedding_coverage(profile_id)


def ensure_profile_hnsw_index(
    profile_id: str,
    *,
    concurrently: bool = True,
) -> ProfileIndexSpec:
    from regwatch.store import embedding_profiles

    return embedding_profiles.ensure_profile_hnsw_index(
        profile_id,
        concurrently=concurrently,
    )


def profile_hnsw_index_ready(profile_id: str) -> bool:
    from regwatch.store import embedding_profiles

    return embedding_profiles.profile_hnsw_index_ready(profile_id)


def assert_profile_ready_for_activation(profile_id: str) -> EmbeddingProfile:
    from regwatch.store import embedding_profiles

    return embedding_profiles.assert_profile_ready_for_activation(profile_id)


def similarity_search_profile(
    profile_id: str,
    query_embedding: list[float],
    *,
    k: int = 8,
    where: dict[str, Any] | None = None,
    mode: RetrievalMode | None = None,
) -> list[Hit]:
    from regwatch.store import embedding_profiles

    return embedding_profiles.similarity_search_profile(
        profile_id,
        query_embedding,
        k=k,
        where=where,
        mode=mode,
    )
