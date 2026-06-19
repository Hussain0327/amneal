"""Optional cross-encoder rerank (Phase-2 / off by default).

Disabled by default. Enable with `RERANKER_ENABLED=true`. We do NOT auto-load
the model in tests or in the main runtime path. This module is here so the
hook point exists for the IT team to slot in their own reranker later.
"""

from __future__ import annotations

from threading import Lock
from typing import Any

from config.settings import get_settings

from regwatch.retrieve.retriever import RetrievedPassage

_RERANKER_MODEL: Any = None
_RERANKER_LOCK = Lock()


def rerank_passages(query: str, passages: list[RetrievedPassage]) -> list[RetrievedPassage]:
    # Read the toggle via Settings (RERANKER_ENABLED) so it is documented and
    # validated like every other env knob, instead of a bare os.getenv read.
    if not get_settings().reranker_enabled:
        return passages
    # Cross-encoder rerank is intentionally not loaded by default to keep cold
    # start fast and CI light. When enabled, swap in your model here.
    try:
        from sentence_transformers import CrossEncoder
    except Exception:
        return passages
    global _RERANKER_MODEL
    if _RERANKER_MODEL is None:
        with _RERANKER_LOCK:
            if _RERANKER_MODEL is None:
                _RERANKER_MODEL = CrossEncoder("BAAI/bge-reranker-base")
    model = _RERANKER_MODEL
    pairs = [(query, p.text) for p in passages]
    scores = model.predict(pairs)
    scored = sorted(zip(passages, scores, strict=False), key=lambda x: -float(x[1]))
    return [p for p, _ in scored]
