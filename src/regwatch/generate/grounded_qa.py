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
from dataclasses import asdict, dataclass, field
from typing import Any

from config.settings import get_settings
from sqlalchemy import func
from sqlmodel import select

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
from regwatch.retrieve.resolver import resolve_brand, resolve_product, suggest_products
from regwatch.retrieve.retriever import RetrievedPassage, retrieve
from regwatch.store.db import session_scope
from regwatch.store.models import PsgDocument

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
class ClarifyOption:
    """A clickable follow-up: a plain-language label + the query to resubmit."""

    label: str
    query: str
    filters: dict[str, Any] | None = None


@dataclass
class QAResult:
    answer: str
    citations: list[Citation]
    refused: bool
    model_name: str
    audit_id: int
    retrieved: list[dict[str, Any]]
    # status supersedes the answer/refuse binary: "clarify" means we know the
    # product but need direction (offer `clarify` options) rather than guess.
    status: str = "answer"  # "answer" | "clarify" | "refused"
    interpretation: str | None = None
    clarify: list[ClarifyOption] = field(default_factory=list)


# Filler words that carry no topic; if a question is only these plus the drug
# name, it's effectively a bare drug name and we should guide, not auto-answer.
_FILLER = frozenset(
    {
        "i",
        "need",
        "help",
        "on",
        "with",
        "about",
        "me",
        "please",
        "info",
        "information",
        "tell",
        "the",
        "a",
        "an",
        "for",
        "of",
        "what",
        "whats",
        "can",
        "you",
        "could",
        "would",
        "is",
        "are",
        "do",
        "does",
        "my",
        "we",
        "want",
        "to",
        "know",
        "give",
        "show",
        "more",
        "something",
        "else",
        "question",
        "hi",
        "hello",
        "regarding",
        "re",
        "some",
        "any",
        "this",
        "that",
        "guidance",
        "drug",
        "fda",
        "anything",
    }
)


def _looks_vague(question: str, normalized_name: str) -> bool:
    """True when the question minus the drug name and filler has no real topic.

    Deterministic (no LLM), so the "bare drug name -> clarify" hero path is
    unit-testable; the live ``model_refusal`` net handles everything subtler.
    """
    drug_tokens = {t for t in re.split(r"[^a-z0-9]+", normalized_name.lower()) if t}
    residual = [
        t
        for t in re.split(r"[^a-z0-9]+", question.lower())
        if t and t not in drug_tokens and t not in _FILLER
    ]
    return not residual


def _doc_count(normalized_name: str) -> int:
    with session_scope() as s:
        return int(
            s.scalar(
                select(func.count())
                .select_from(PsgDocument)
                .where(PsgDocument.normalized_name == normalized_name)
            )
            or 0
        )


def _interpretation_for(normalized_name: str) -> str:
    n = _doc_count(normalized_name)
    docs = "document" if n == 1 else "documents"
    have = (
        f"FDA has {n} product-specific guidance {docs} for it" if n else "I have its FDA guidance"
    )
    return (
        f"You're asking about {normalized_name.title()}. {have} — " "what would you like to know?"
    )


def build_options(normalized_name: str) -> list[ClarifyOption]:
    """Plain-language things we can actually answer for a resolved product.

    These are QUESTION TEMPLATES that re-run retrieval — they do not read the
    (possibly empty) BeRequirement table — so they work on the full catalog.
    """
    nm = normalized_name
    flt = {"normalized_name": nm}
    return [
        ClarifyOption(
            "Recommended bioequivalence (BE) study — how FDA wants a generic shown equivalent",
            f"What bioequivalence study design does FDA recommend for {nm}?",
            flt,
        ),
        ClarifyOption(
            "Dissolution method",
            f"What dissolution method does the FDA guidance recommend for {nm}?",
            flt,
        ),
        ClarifyOption(
            "Strengths and dosage forms covered",
            f"What strengths and dosage forms does the FDA guidance cover for {nm}?",
            flt,
        ),
    ]


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
        status="refused",
    )


def _clarify(
    *,
    question: str,
    reason: str,
    model_name: str,
    interpretation: str,
    options: list[ClarifyOption],
) -> QAResult:
    """Guide instead of guess: we know the product (or a near-match) but need
    direction. Carries ZERO citations (never fabricates) and logs one audit row
    (INV-6), exactly like ``_refuse``."""
    audit_id = log_query(
        mode="qa",
        query_text=question,
        retrieved=[],
        answer_text=interpretation,
        citations=[],
        refused=False,
        model_name=model_name,
    )
    log.info("qa_clarify", reason=reason, audit_id=audit_id, options=len(options))
    return QAResult(
        answer=interpretation,
        citations=[],
        refused=False,
        model_name=model_name,
        audit_id=audit_id,
        retrieved=[],
        status="clarify",
        interpretation=interpretation,
        clarify=options,
    )


def ask(
    question: str,
    *,
    filters: dict[str, Any] | None = None,
    k: int | None = None,
) -> QAResult:
    """Grounded Q&A entry point — answer with citations, clarify, or refuse."""
    s = get_settings()
    model_name = current_model_name(role="synthesizer")
    active_filters: dict[str, Any] = dict(filters or {})
    resolved_by_name = False

    # Entity resolution FIRST: pin the product before semantic retrieval so FDA
    # template boilerplate shared across drugs cannot leak a wrong-drug citation.
    # Skip only when the caller already pinned the product (API / dossier).
    if not active_filters.get("normalized_name"):
        resolution = resolve_product(question)
        if resolution.status == "resolved":
            active_filters["normalized_name"] = resolution.normalized_name
            resolved_by_name = resolution.by_name
        elif resolution.status == "ambiguous":
            # Several products match → ASK which, don't guess (cross-drug guard).
            return _clarify(
                question=question,
                reason="ambiguous_product",
                model_name=model_name,
                interpretation="More than one product matches that. Which did you mean?",
                options=[
                    ClarifyOption(name.title(), name, {"normalized_name": name})
                    for name in resolution.candidates
                ],
            )
        else:
            # No product named. Offer a high-confidence "did you mean" for genuine
            # typos, then a brand→generic lookup (Adderall → amphetamine); else
            # refuse (e.g. romidepsin — absent, a deliberate must-refuse).
            suggestions = suggest_products(question)
            if suggestions:
                return _clarify(
                    question=question,
                    reason="did_you_mean",
                    model_name=model_name,
                    interpretation="I couldn't find that exact drug. Did you mean:",
                    options=[
                        ClarifyOption(name.title(), name, {"normalized_name": name})
                        for name in suggestions
                    ],
                )
            brand_matches = resolve_brand(question)
            if brand_matches:
                return _clarify(
                    question=question,
                    reason="brand_lookup",
                    model_name=model_name,
                    interpretation=(
                        "That looks like a brand name. Did you mean its generic ingredient?"
                    ),
                    options=[
                        ClarifyOption(name.title(), name, {"normalized_name": name})
                        for name in brand_matches
                    ],
                )
            return _refuse(
                question=question, passages=[], reason="no_product", model_name=model_name
            )

    resolved_name = active_filters.get("normalized_name")

    # Bare drug name / no real question → guide with options instead of dumping a
    # default BE answer. Deterministic, pre-LLM (the unit-testable hero path).
    if resolved_name and resolved_by_name and _looks_vague(question, resolved_name):
        return _clarify(
            question=question,
            reason="vague_input",
            model_name=model_name,
            interpretation=_interpretation_for(resolved_name),
            options=build_options(resolved_name),
        )

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
        # The user named a real drug but the model couldn't answer this phrasing
        # (the live net for vague inputs `_looks_vague` didn't catch) → guide.
        # When the product came from the single-product fallback (no drug named),
        # a model refusal is a genuine "not covered" → stay refused (INV-2).
        if resolved_by_name and resolved_name:
            return _clarify(
                question=question,
                reason="model_refusal",
                model_name=response.model,
                interpretation=_interpretation_for(resolved_name),
                options=build_options(resolved_name),
            )
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
