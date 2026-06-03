"""Retrieval — embed the query, return top-k chunks with metadata + scores.

Filters supported via the `filters` arg (passed through to the vector store):
  - normalized_name (exact match)
  - dosage_form    (exact match)
  - route          (exact match)
  - psg_type       ("draft" | "final")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.settings import get_settings

from regwatch.process.embedder import get_embedding_provider
from regwatch.store.vector_store import Hit, similarity_search


@dataclass
class RetrievedPassage:
    """A passage we will (a) optionally cite in an answer and (b) audit."""

    chunk_id: str
    text: str
    score: float
    doc_id: int
    version_id: int
    page: int
    section_path: str | None
    normalized_name: str
    source_url: str
    short_name: str  # what citations use, e.g. "PSG_020503"
    metadata: dict[str, Any]


def _short_name(meta: dict[str, Any]) -> str:
    """A short, citation-friendly label for the document."""
    appl = meta.get("appl_no") or ""
    if appl:
        return f"PSG_{appl}"
    name = (meta.get("normalized_name") or "PSG").strip()
    return name.replace(" ", "_") or "PSG"


def _build_where(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert flat filters to Chroma's `where` syntax."""
    if not filters:
        return None
    out: dict[str, Any] = {}
    for k, v in filters.items():
        if v in (None, "", []):
            continue
        out[k] = {"$eq": v}
    if not out:
        return None
    if len(out) == 1:
        return out
    return {"$and": [{k: v} for k, v in out.items()]}


def retrieve(
    query: str,
    *,
    k: int | None = None,
    filters: dict[str, Any] | None = None,
) -> list[RetrievedPassage]:
    """Stage-1 vector search.

    Returns up to VECTOR_TOP_K candidates. The reranker (in grounded_qa) trims
    this to RERANK_TOP_K. If the caller passes an explicit `k`, that overrides
    VECTOR_TOP_K for the wide-net stage.
    """
    s = get_settings()
    k = k or s.vector_top_k
    embedder = get_embedding_provider()
    qv = embedder.embed([query])[0]
    where = _build_where(filters)
    hits: list[Hit] = similarity_search(qv, k=k, where=where)

    passages: list[RetrievedPassage] = []
    for h in hits:
        meta = h.metadata or {}
        passages.append(
            RetrievedPassage(
                chunk_id=h.chunk_id,
                text=h.text,
                score=h.score,
                doc_id=int(meta.get("doc_id") or 0),
                version_id=int(meta.get("version_id") or 0),
                page=int(meta.get("page") or 0),
                section_path=meta.get("section_path") or None,
                normalized_name=str(meta.get("normalized_name") or ""),
                source_url=str(meta.get("source_url") or ""),
                short_name=_short_name(meta),
                metadata=meta,
            )
        )
    return passages
