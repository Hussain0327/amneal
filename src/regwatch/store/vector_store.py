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


def add_chunks(
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    if not ids:
        return
    coll = get_collection()
    coll.upsert(
        ids=ids,
        embeddings=embeddings,  # type: ignore[arg-type]
        documents=documents,
        metadatas=metadatas,  # type: ignore[arg-type]
    )


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
