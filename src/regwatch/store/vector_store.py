"""Thin wrapper around ChromaDB.

Chunks live in a single collection. Each chunk's metadata carries enough to
build a citation (`doc_id`, `version_id`, `page`, `source_url`) and enough to
filter by drug (`normalized_name`, `dosage_form`, `route`).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings
from config.settings import get_settings

COLLECTION = "regwatch_chunks"


@dataclass
class Hit:
    chunk_id: str
    text: str
    metadata: dict[str, Any]
    score: float  # cosine similarity in [0, 1] (we convert from distance)


_client: chromadb.api.ClientAPI | None = None
_metadata_values_cache: dict[str, frozenset[str]] = {}


def get_client() -> chromadb.api.ClientAPI:
    global _client
    if _client is None:
        s = get_settings()
        s.ensure_dirs()
        _client = chromadb.PersistentClient(
            path=s.chroma_dir.as_posix(),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
    return _client


def get_collection() -> Collection:
    client = get_client()
    return client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def reset_for_tests() -> None:
    global _client
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.reset()
    _client = None
    _metadata_values_cache.clear()


def add_chunks(
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    if not ids:
        return
    _metadata_values_cache.clear()
    coll = get_collection()
    coll.upsert(
        ids=ids,
        embeddings=embeddings,  # type: ignore[arg-type]
        documents=documents,
        metadatas=metadatas,  # type: ignore[arg-type]
    )


def delete_chunks_for_doc_except_version(doc_id: int, keep_version_id: int) -> int:
    """Delete indexed chunks for one PSG document except the current version.

    SQLite remains the version audit store. Chroma is only the current-answer
    search index, so old chunks for the same PSG document should not remain
    retrievable after a revision.
    """
    coll = get_collection()
    res = coll.get(
        where={"doc_id": {"$eq": doc_id}},  # type: ignore[arg-type, dict-item]
        include=["metadatas"],  # type: ignore[list-item]
    )
    ids_to_delete: list[str] = []
    for chunk_id, meta in zip(res.get("ids") or [], res.get("metadatas") or [], strict=False):
        raw_version: object = (meta or {}).get("version_id") if isinstance(meta, dict) else None
        try:
            version_id = int(raw_version) if isinstance(raw_version, str | int | float) else 0
        except (TypeError, ValueError):
            version_id = 0
        if version_id != keep_version_id:
            ids_to_delete.append(chunk_id)

    if not ids_to_delete:
        return 0
    coll.delete(ids=ids_to_delete)
    _metadata_values_cache.clear()
    return len(ids_to_delete)


def similarity_search(
    query_embedding: list[float],
    *,
    k: int = 8,
    where: dict[str, Any] | None = None,
) -> list[Hit]:
    coll = get_collection()
    res = coll.query(
        query_embeddings=[query_embedding],  # type: ignore[arg-type]
        n_results=k,
        where=where,  # type: ignore[arg-type]
        include=["documents", "metadatas", "distances"],  # type: ignore[list-item]
    )
    hits: list[Hit] = []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    for i, chunk_id in enumerate(ids):
        # Chroma cosine distance is in [0, 2]; convert to similarity in [0, 1].
        sim = 1.0 - float(dists[i]) / 2.0
        sim = max(0.0, min(1.0, sim))  # clamp for float-precision overshoot
        hits.append(Hit(chunk_id=chunk_id, text=docs[i], metadata=dict(metas[i] or {}), score=sim))
    return hits


def collection_size() -> int:
    return int(get_collection().count())


def distinct_metadata_values(key: str) -> set[str]:
    """All distinct non-empty string values of one metadata `key` across chunks.

    Used by the product resolver to learn which drugs the corpus can answer
    about. Cached because a full Chroma metadata scan is too expensive once the
    PSG corpus grows beyond the POC seed set. `add_chunks` and test resets
    invalidate this cache.
    """
    cached = _metadata_values_cache.get(key)
    if cached is not None:
        return set(cached)
    got = get_collection().get(include=["metadatas"])
    out: set[str] = set()
    for meta in got.get("metadatas") or []:
        value = (meta or {}).get(key)
        if isinstance(value, str) and value:
            out.add(value)
    _metadata_values_cache[key] = frozenset(out)
    return out
