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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # typing-only import to avoid a cycle with pgvector_store
    from sqlalchemy.engine import Connection


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
    embeddings: list[list[float]],
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


def delete_chunks_for_doc_except_version(doc_id: int, keep_version_id: int) -> int:
    """Delete indexed chunks for one PSG document except the current version.

    Old chunks for the same PSG document must not remain retrievable after a
    revision — the chunk table is the current-answer search index; psg_version
    stays the audit store.
    """
    from regwatch.store import pgvector_store

    return pgvector_store.delete_chunks_for_doc_except_version(doc_id, keep_version_id)


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


def distinct_metadata_values(key: str) -> set[str]:
    """All distinct non-empty string values of one metadata `key` across chunks.

    Used by the product resolver to learn which drugs the corpus can answer
    about (pgvector_store caches this with a TTL; `add_chunks` and test resets
    invalidate it).
    """
    from regwatch.store import pgvector_store

    return pgvector_store.distinct_metadata_values(key)
