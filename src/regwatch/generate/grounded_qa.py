"""Grounded Q&A orchestration.

Flow:
  1. Retrieve top-k passages.
  2. If top-1 score < REFUSAL_SCORE_THRESHOLD → refuse (do NOT call the LLM).
  3. Otherwise, call the LLM with the strict grounding system prompt and the
     retrieved passages.
  4. Parse the answer. If the LLM emitted the refusal string → refuse.
  5. Validate citations: every `[short_name, p.N]` in the answer must
     correspond to a passage we actually sent. Unknown citations are stripped
     to a warning; if the answer has NO valid citations AND it contains
     content, fall back to refusal.
  6. Write an audit log row (INV-6) regardless of outcome and return.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from config.settings import get_settings

from regwatch.common.audit import log_query
from regwatch.common.citations import (
    filter_citations,
    iter_psg_citations,
    strip_all_citations,
)
from regwatch.common.logging import get_logger
from regwatch.generate.llm import LLMMessage, current_model_name, get_llm_provider
from regwatch.generate.prompts import GROUNDED_QA_SYSTEM, GROUNDED_QA_USER
from regwatch.retrieve.reranker import rerank_passages
from regwatch.retrieve.resolver import resolve_product
from regwatch.retrieve.retriever import RetrievedPassage, retrieve

log = get_logger(__name__)


@dataclass
class Citation:
    short_name: str
    page: int
    chunk_id: str
    doc_id: int
    version_id: int
    source_url: str
    snippet: str


@dataclass
class QAResult:
    answer: str
    citations: list[Citation]
    refused: bool
    model_name: str
    audit_id: int
    retrieved: list[dict[str, Any]]


def _format_passages(passages: list[RetrievedPassage]) -> str:
    blocks: list[str] = []
    for p in passages:
        section = f" ({p.section_path})" if p.section_path else ""
        blocks.append(f"[{p.short_name}, p.{p.page}]{section}\n{p.text.strip()}\n")
    return "\n---\n".join(blocks)


def _validate_citations(
    answer_text: str, passages: list[RetrievedPassage]
) -> tuple[list[Citation], list[tuple[str, int]]]:
    """Return (validated citations in order of appearance, list of bad cites)."""
    allowed: dict[tuple[str, int], RetrievedPassage] = {}
    for p in passages:
        allowed[(p.short_name, p.page)] = p

    seen: set[tuple[str, int]] = set()
    validated: list[Citation] = []
    bad: list[tuple[str, int]] = []

    for short_name, page in iter_psg_citations(answer_text):
        key = (short_name, page)
        passage = allowed.get(key)
        if passage is None:
            bad.append(key)
            continue
        if key in seen:
            continue
        seen.add(key)
        snippet = passage.text.strip().replace("\n", " ")[:200]
        validated.append(
            Citation(
                short_name=short_name,
                page=page,
                chunk_id=passage.chunk_id,
                doc_id=passage.doc_id,
                version_id=passage.version_id,
                source_url=passage.source_url,
                snippet=snippet,
            )
        )
    return validated, bad


def _refuse(
    *,
    question: str,
    passages: list[RetrievedPassage],
    reason: str,
    model_name: str,
) -> QAResult:
    s = get_settings()
    audit_id = log_query(
        mode="qa",
        query_text=question,
        retrieved=[
            {
                "chunk_id": p.chunk_id,
                "score": p.score,
                "doc_id": p.doc_id,
                "page": p.page,
                "normalized_name": p.normalized_name,
                "short_name": p.short_name,
            }
            for p in passages
        ],
        answer_text=s.refusal_text,
        citations=[],
        refused=True,
        model_name=model_name,
    )
    log.info("qa_refused", reason=reason, audit_id=audit_id)
    return QAResult(
        answer=s.refusal_text,
        citations=[],
        refused=True,
        model_name=model_name,
        audit_id=audit_id,
        retrieved=[
            {
                "chunk_id": p.chunk_id,
                "score": p.score,
                "page": p.page,
                "short_name": p.short_name,
            }
            for p in passages
        ],
    )


def ask(
    question: str,
    *,
    filters: dict[str, Any] | None = None,
    k: int | None = None,
) -> QAResult:
    """Grounded Q&A entry point — answer with citations, or refuse."""
    s = get_settings()
    model_name = current_model_name(role="synthesizer")
    active_filters: dict[str, Any] = dict(filters or {})

    # Entity resolution FIRST: pin the product before semantic retrieval so FDA
    # template boilerplate shared across drugs cannot leak a wrong-drug citation.
    # Skip only when the caller already pinned the product (API / dossier).
    if not active_filters.get("normalized_name"):
        resolution = resolve_product(question)
        if resolution.status == "resolved":
            active_filters["normalized_name"] = resolution.normalized_name
        else:
            # Ambiguous or unresolvable for a product-specific corpus → never
            # answer globally (the cross-drug-leak guard): refuse over guessing
            # the drug. Clarify options are surfaced by the orchestration layer.
            reason = "ambiguous_product" if resolution.status == "ambiguous" else "no_product"
            return _refuse(question=question, passages=[], reason=reason, model_name=model_name)

    # Stage 1: wide-net vector search (up to VECTOR_TOP_K), constrained to the product.
    passages = retrieve(question, k=k, filters=active_filters)
    # Stage 2: optional rerank, then trim to RERANK_TOP_K.
    passages = rerank_passages(question, passages)
    passages = passages[: s.effective_rerank_top_k]

    # INV-2: if retrieval is weak, refuse before calling the LLM.
    if not passages or passages[0].score < s.refusal_score_threshold:
        return _refuse(
            question=question,
            passages=passages,
            reason="low_top_score",
            model_name=model_name,
        )

    # Post-retrieval guard (defense in depth): every passage must be the same
    # product. The filter guarantees this; this catches a caller that bypassed
    # the resolver. Mixed products → collapse to refusal rather than cite across.
    if len({p.normalized_name for p in passages if p.normalized_name}) > 1:
        return _refuse(
            question=question,
            passages=passages,
            reason="mixed_products",
            model_name=model_name,
        )

    user_prompt = GROUNDED_QA_USER.format(
        question=question,
        passages=_format_passages(passages),
    )
    system_prompt = GROUNDED_QA_SYSTEM.format(refusal=s.refusal_text)

    provider = get_llm_provider(role="synthesizer")
    response = provider.complete(
        [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_prompt),
        ],
        temperature=0.0,
        max_tokens=900,
    )
    answer = response.text.strip()

    # LLM-side refusal: it returned the exact refusal sentinel.
    if answer == s.refusal_text or answer.startswith(s.refusal_text):
        return _refuse(
            question=question,
            passages=passages,
            reason="model_refusal",
            model_name=response.model,
        )

    citations, bad = _validate_citations(answer, passages)
    if bad:
        log.warning("qa_unknown_citations", bad=bad)

    # INV-1: if the answer has body text but no valid citations, refuse rather
    # than emit an ungrounded answer.
    answer_body = strip_all_citations(answer).strip()
    if answer_body and not citations:
        return _refuse(
            question=question,
            passages=passages,
            reason="no_valid_citations",
            model_name=response.model,
        )

    # INV-1: strip any fabricated citation markers from the prose so the
    # rendered answer never shows an unverifiable citation. Valid markers are
    # kept intact; only those whose (short_name, page) is not in the validated
    # set are removed (compound brackets keep just their valid pairs).
    valid_keys = {(c.short_name, c.page) for c in citations}
    cleaned_answer = filter_citations(answer, valid_keys)
    # Tidy whitespace left behind by removed markers.
    cleaned_answer = re.sub(r"\s+([.,;:])", r"\1", cleaned_answer)
    cleaned_answer = re.sub(r"[ \t]{2,}", " ", cleaned_answer).strip()

    audit_id = log_query(
        mode="qa",
        query_text=question,
        retrieved=[
            {
                "chunk_id": p.chunk_id,
                "score": p.score,
                "doc_id": p.doc_id,
                "page": p.page,
                "normalized_name": p.normalized_name,
                "short_name": p.short_name,
            }
            for p in passages
        ],
        answer_text=cleaned_answer,
        citations=[asdict(c) for c in citations],
        refused=False,
        model_name=response.model,
    )
    return QAResult(
        answer=cleaned_answer,
        citations=citations,
        refused=False,
        model_name=response.model,
        audit_id=audit_id,
        retrieved=[
            {
                "chunk_id": p.chunk_id,
                "score": p.score,
                "page": p.page,
                "short_name": p.short_name,
            }
            for p in passages
        ],
    )
