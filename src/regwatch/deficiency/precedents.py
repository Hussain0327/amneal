"""Precedent retrieval for deficiency detection (D1-safe).

Upstream DefPredict's ``retrieval.knowledge_base.find_similar_deficiencies``
embedded queries locally (bge-m3) and searched FAISS. Here the KB lives in
Postgres (``deficiency_kb``, vector(1024)) and queries embed through the
Databricks Qwen3 endpoint -- the same in-tenant plane as generation -- so text
derived from an uploaded submission never reaches OpenAI.

Callers (detection/selection.py ``gather_precedents``) already treat any
exception as best-effort-absent precedents, so this module raises freely on
misconfiguration; the one deliberate soft path is the empty-KB guard, which
skips the embedding call entirely while the roadmap spreadsheet has not been
loaded yet.
"""

from __future__ import annotations

from config.settings import get_settings

from regwatch.common.logging import get_logger
from regwatch.deficiency.schemas.flaws import SimilarDeficiency
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.deficiency_kb import KB_EMBEDDING_DIM, kb_count, search_similar

log = get_logger(__name__)


def find_similar_deficiencies(query_text: str, top_k: int | None = None) -> list[SimilarDeficiency]:
    k = top_k if top_k is not None else get_settings().deficiency_precedent_top_k
    if k <= 0 or not query_text.strip():
        return []
    if kb_count() == 0:
        log.info("deficiency_kb_empty")
        return []
    provider = get_embedding_provider("openai")
    if int(provider.dim) != KB_EMBEDDING_DIM:
        raise RuntimeError(
            f"OpenAI embedding provider is configured for {provider.dim} dims; "
            f"deficiency_kb stores vector({KB_EMBEDDING_DIM})"
        )
    matches = search_similar(provider.embed_query(query_text), top_k=k)
    return [
        SimilarDeficiency(
            anda_number=m.anda_number,
            product_name=m.product_name,
            deficiency_text=m.deficiency_text,
            similarity_score=m.score,
        )
        for m in matches
    ]
