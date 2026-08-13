"""Batched, resumable embedding backfill scoped to authoritative FDA chunks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import text as sa_text

from regwatch.store.db import get_engine
from regwatch.store.vector_store import (
    get_embedding_profile,
    update_legacy_chunk_embeddings,
    upsert_profile_embeddings,
)


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


def pending_corpus_chunks(profile_id: str, *, limit: int = 128) -> list[PendingCorpusChunk]:
    """Return one deterministic pending page from the FDA namespace only."""

    normalized = profile_id.strip()
    if limit <= 0:
        return []
    params: dict[str, object] = {"limit": int(limit)}
    if normalized == "legacy":
        query = sa_text(
            "SELECT c.id, c.text FROM chunk c "
            "WHERE c.fda_document_id IS NOT NULL AND c.text IS NOT NULL "
            "AND c.embedding IS NULL ORDER BY c.id LIMIT :limit"
        )
    else:
        get_embedding_profile(normalized)
        query = sa_text(
            "SELECT c.id, c.text FROM chunk c "
            "LEFT JOIN chunk_embedding ce ON ce.chunk_id = c.id "
            "AND ce.profile_id = :profile_id "
            "WHERE c.fda_document_id IS NOT NULL AND c.text IS NOT NULL "
            "AND ce.chunk_id IS NULL ORDER BY c.id LIMIT :limit"
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
    if normalized == "legacy":
        update_legacy_chunk_embeddings(
            [chunk.chunk_id for chunk in chunks],
            [chunk.text for chunk in chunks],
            embeddings,
        )
        return
    upsert_profile_embeddings(
        normalized,
        [chunk.chunk_id for chunk in chunks],
        embeddings,
        [chunk.content_hash for chunk in chunks],
    )


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
