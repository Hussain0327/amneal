"""Retrieval — embed the query, return top-k chunks with metadata + scores.

Filters supported via the `filters` arg (passed through to the vector store):
  - normalized_name (exact match; callers canonicalize to lowercase)
  - dosage_form    (exact, case-insensitive match)
  - route          (exact, case-insensitive match)
  - psg_type       ("draft" | "final", case-insensitive)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.settings import get_settings
from sqlalchemy import desc, func, inspect
from sqlmodel import col, select

from regwatch.process.embedder import (
    embed_query,
    get_embedding_provider,
    get_embedding_provider_for_profile,
)
from regwatch.retrieve.mode import RetrievalMode, RetrievalScope, default_mode_for_scope
from regwatch.store.db import get_engine, session_scope
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.store.vector_store import (
    Hit,
    distinct_metadata_values,
    get_embedding_profile,
    similarity_search,
    similarity_search_profile,
)


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


# Catalog fields stored verbatim from the FDA listing ("Aerosol, Metered",
# "Inhalation") or lowercased at ingest ("draft"). Hand-typed UI filters arrive
# in whatever casing the user chose, and the vector-store `where` is exact-match,
# so these values are folded to the stored casing before filtering.
_CASE_FOLDED_FILTER_KEYS = ("dosage_form", "route", "psg_type")


def _fold_filter_casing(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map case-variant filter values to the corpus's stored casing.

    Uses the TTL-cached distinct metadata values, so the common no-filter path
    pays nothing. A value with no case-insensitive stored match (or an ambiguous
    one) passes through verbatim -- it matches nothing, exactly as before, and
    the caller's no-passages handling applies.
    """
    if not filters or not any(
        isinstance(filters.get(key), str) and filters.get(key) for key in _CASE_FOLDED_FILTER_KEYS
    ):
        return filters
    folded = dict(filters)
    for key in _CASE_FOLDED_FILTER_KEYS:
        value = folded.get(key)
        if not isinstance(value, str) or not value:
            continue
        stored = {v for v in distinct_metadata_values(key) if v.lower() == value.lower()}
        if len(stored) == 1:
            folded[key] = next(iter(stored))
    return folded


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


def _and_where(*clauses: dict[str, Any] | None) -> dict[str, Any] | None:
    active = [c for c in clauses if c]
    if not active:
        return None
    if len(active) == 1:
        return active[0]
    flattened: list[dict[str, Any]] = []
    for clause in active:
        if set(clause) == {"$and"} and isinstance(clause["$and"], list):
            flattened.extend(clause["$and"])
        else:
            flattened.append(clause)
    if len(flattened) == 1:
        return flattened[0]
    return {"$and": flattened}


def _current_version_ids_for_filters(filters: dict[str, Any] | None) -> list[int] | None:
    """Return current PSG version ids matching filters, or None in vector-only mode.

    Some unit tests seed Chroma directly without a SQLite PSG catalog. In the real
    app, once `psg_document` exists, normal retrieval must be scoped to the latest
    `psg_version` rows so superseded chunks cannot be cited.
    """
    engine = get_engine()
    inspector = inspect(engine)
    if not inspector.has_table("psg_document") or not inspector.has_table("psg_version"):
        return None

    filters = filters or {}
    with session_scope() as s:
        doc_count = int(s.scalar(select(func.count()).select_from(PsgDocument)) or 0)
        if doc_count == 0:
            return None

        doc_stmt = select(PsgDocument.id)
        if filters.get("doc_id"):
            try:
                doc_id = int(filters["doc_id"])
            except (TypeError, ValueError):
                return []
            doc_stmt = doc_stmt.where(PsgDocument.id == doc_id)
        if filters.get("normalized_name"):
            doc_stmt = doc_stmt.where(
                PsgDocument.normalized_name == str(filters["normalized_name"])
            )
        # Case-insensitive on both sides (works on SQLite and Postgres): the
        # catalog stores FDA listing casing while UI filters are hand-typed.
        if filters.get("dosage_form"):
            doc_stmt = doc_stmt.where(
                func.lower(PsgDocument.dosage_form) == str(filters["dosage_form"]).lower()
            )
        if filters.get("route"):
            doc_stmt = doc_stmt.where(
                func.lower(PsgDocument.route) == str(filters["route"]).lower()
            )
        if filters.get("psg_type"):
            doc_stmt = doc_stmt.where(
                func.lower(PsgDocument.psg_type) == str(filters["psg_type"]).lower()
            )

        doc_ids = [int(doc_id) for doc_id in s.scalars(doc_stmt) if doc_id is not None]
        if not doc_ids:
            return []

        version_rows = s.execute(
            select(PsgVersion.psg_document_id, PsgVersion.id)
            .where(col(PsgVersion.psg_document_id).in_(doc_ids))
            .order_by(
                col(PsgVersion.psg_document_id),
                desc(col(PsgVersion.captured_at)),
                desc(col(PsgVersion.id)),
            )
        ).all()

    current: dict[int, int] = {}
    for doc_id, version_id in version_rows:
        if doc_id is None or version_id is None:
            continue
        current.setdefault(int(doc_id), int(version_id))
    return list(current.values())


def retrieve(
    query: str,
    *,
    k: int | None = None,
    filters: dict[str, Any] | None = None,
    mode: RetrievalMode | None = None,
) -> list[RetrievedPassage]:
    """Stage-1 vector search.

    Returns up to VECTOR_TOP_K candidates. The reranker (in grounded_qa) trims
    this to RERANK_TOP_K. If the caller passes an explicit `k`, that overrides
    VECTOR_TOP_K for the wide-net stage.

    ``mode`` is the CALLER's choice of retrieval algorithm. Omitted, it defaults
    from the resolved scope -- always to one of the EXACT modes, so no caller
    gets approximate search by accident. The mode fully determines the SQL and
    the session settings (see store.embedding_profiles.build_search_sql), which
    is why the caller can record what will run without the store handing back a
    separate execution report.

    The scope is derived from the CALLER's filters, deliberately BEFORE the
    current-version clause is added: that clause goes on every query, so "has a
    filter" cannot tell a product-scoped question from a corpus-wide one -- the
    distinction that actually matters.
    """
    s = get_settings()
    k = k if k is not None else s.vector_top_k
    if k <= 0:
        return []
    resolved_mode = mode or default_mode_for_scope(RetrievalScope.from_filters(filters))
    profile_id = (s.active_embedding_profile or "legacy").strip()
    if profile_id == "legacy":
        embedder = get_embedding_provider()
    else:
        profile = get_embedding_profile(profile_id)
        embedder = get_embedding_provider_for_profile(profile)
    qv = embed_query(embedder, query)
    filters = _fold_filter_casing(filters)
    where = _build_where(filters)
    # An explicit version_id filter (internal callers only -- the API whitelists
    # it out of external input) targets one specific version, so the current-
    # version scoping below would be contradictory; everything else is scoped.
    if not (filters or {}).get("version_id"):
        current_version_ids = _current_version_ids_for_filters(filters)
        if current_version_ids is not None:
            if not current_version_ids:
                return []
            where = _and_where(where, {"version_id": {"$in": current_version_ids}})
    if profile_id == "legacy":
        hits: list[Hit] = similarity_search(qv, k=k, where=where)
    else:
        hits = similarity_search_profile(profile_id, qv, k=k, where=where, mode=resolved_mode)

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
