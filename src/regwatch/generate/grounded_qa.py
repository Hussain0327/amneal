"""Grounded Q&A orchestration.

Last updated: 2026-08-11.

Flow:
  1. Resolve product/form and retrieve top-k passages when the turn is answerable.
  2. Every healthy Ask turn reaches one AI path: constrained query guidance for
     a pre-synthesis non-answer outcome, or grounded synthesis for evidence.
  3. The synthesis path sends only above-threshold evidence with the grounding
     prompt. Which prompt depends on two flags, read per turn:
       v7 (prose + selective citation)  what prod serves today,
       v6 (prose, cite or refuse)       prose flag on, selective flag off,
       v5 (claims JSON + schema)        both off.
  4. Hand the completion to the admission gate. Under v5 it goes straight to
     ``turn_gate.admit_turn``; under v6/v7 ``prose_turn.parse`` first turns the
     prose into claims and resolves each [n] marker to a passage POSITION, so
     the gate still validates against the passages actually sent this turn.
  5. Dispatch on the gate's verdict: render the admitted claims, decline, or,
     when the payload did not parse, serve the service-error copy.
  6. Write an audit log row (INV-6) regardless of outcome and return.

The model never writes a citation marker that reaches a user. Under v5 it
declares (short_name, page) pairs; under v6/v7 it writes [n] and the parser
resolves the number to a passage. Either way the renderer writes every
canonical marker from a validated passage (see generate/turn_gate.py).

Strangler Step 2: ``ask_core`` computes steps 1-5 and RETURNS what to persist
(rag_contract dataclasses); the ``ask()`` shell owns step 6 and every other
write. Callers see the unchanged ``ask()`` / ``QAResult`` surface.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from time import perf_counter
from typing import Any

from config.settings import SYNTH_MAX_TOKENS_CEILING, Settings, get_settings
from sqlalchemy import func
from sqlmodel import select

from regwatch.common.audit import log_query
from regwatch.common.citations import strip_all_citations, strip_sources_trailer
from regwatch.common.conversation import (
    SESSION_ORIGIN_THREAD,
    PriorTurn,
    SessionOriginError,
    SessionOwnershipError,
    ensure_session,
    get_recent_turns,
    get_session_filters,
    new_turn_id,
    record_message,
    update_session_filters,
)
from regwatch.common.logging import get_logger
from regwatch.common.observability import capture_exception
from regwatch.common.text_normalize import canonical_name
from regwatch.generate import prose_turn
from regwatch.generate import turn_gate as tg
from regwatch.generate.guidance import (
    QUERY_GUIDANCE_PROMPT,
    build_guidance_request,
    parse_guidance_plan,
    prioritize_options,
    render_guidance_message,
    selected_option_records,
)
from regwatch.generate.llm import (
    D1ResidencyError,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMUsage,
    current_model_name,
    estimate_cost_usd,
    get_llm_provider,
)
from regwatch.generate.prompts import (
    GROUNDED_QA_EXEMPLARS_V6,
    GROUNDED_QA_EXEMPLARS_V7,
    GROUNDED_QA_PROMPT,
    GROUNDED_QA_PROMPT_V6,
    GROUNDED_QA_PROMPT_V7,
    GROUNDED_QA_SYSTEM,
    GROUNDED_QA_SYSTEM_V6,
    GROUNDED_QA_SYSTEM_V7,
    GROUNDED_QA_USER,
    GROUNDED_QA_USER_V6,
    GROUNDED_QA_USER_V7,
)

# The core/shell contract types live in rag_contract (a pipeline-free module so
# they can become a cross-service HTTP contract). Citation / ClarifyOption /
# QueryStatusLiteral are spelled "as" themselves because api.main, the dossier
# stubs, and the tests import them from grounded_qa -- that public surface must
# not move, and mypy strict (no_implicit_reexport) only re-exports the aliased
# form.
from regwatch.generate.rag_contract import (
    AuditPayload,
    ClaimTag,
    RagOutcome,
    SessionPatch,
)
from regwatch.generate.rag_contract import (
    Citation as Citation,
)
from regwatch.generate.rag_contract import (
    ClarifyOption as ClarifyOption,
)
from regwatch.generate.rag_contract import (
    QueryStatusLiteral as QueryStatusLiteral,
)
from regwatch.generate.route import (
    ROUTE_PROMPT,
    CorpusPolicyHint,
    RouteHistoryTurn,
    ScopeHint,
)
from regwatch.generate.route_shadow import (
    RouteShadowObservation,
    finalize_route_observation,
    observe_route,
)
from regwatch.generate.turn_gate import AdmittedTurn, GateFailure, admit_claims, admit_turn
from regwatch.generate.turn_schema import TURN_SCHEMA_MESSAGE
from regwatch.generate.unresolved import classify_unresolved, is_social
from regwatch.retrieve.diversity import mmr_select
from regwatch.retrieve.mode import RetrievalPlan, RetrievalScope, default_mode_for_scope
from regwatch.retrieve.reranker import rerank_passages
from regwatch.retrieve.resolver import lookup_external_drug, resolve_product, suggest_products
from regwatch.retrieve.retriever import RetrievedPassage, retrieve
from regwatch.retrieve.scope import (
    CompiledScope,
    CompiledScopeKind,
    ProductScope,
    compile_scope,
    compiled_scope_from_audit,
)
from regwatch.retrieve.scope_catalog import load_corpus_policy_snapshots
from regwatch.store.db import session_scope
from regwatch.store.models import PsgDocument
from regwatch.store.queries import (
    count_documents,
    current_dosage_form_routes,
    fetch_citation_recency,
    load_prior_corpus_scope,
)
from regwatch.store.vector_store import distinct_metadata_values
from regwatch.watch.alerts import latest_digest_records
from regwatch.watch.watchlist import list_watchlist

log = get_logger(__name__)

# Replay chunk size for the post-audit "typing" effect. ~60 chars is a
# comfortable read cadence and is far larger than any citation marker, so a
# marker is never the thing that forces a chunk boundary.
_REPLAY_CHUNK_CHARS = 60


def _maybe_inject_fault(stage: str) -> None:
    """Prod-fenced fault injection for the R1 contract suite (S24).

    Forces an UNEXPECTED raise in the named pipeline stage so the harness can
    prove the step-5 CompleteQuery audited-error boundary (``compute_turn``)
    turns a retrieve/resolver crash into exactly one status="error" audit row
    instead of the naked unaudited 500 ``ask()`` used to leak. Gated by the
    SAME ``allow_test_providers`` boot guard as the echo/forced-refusal
    providers, so it is inert in production regardless of the env var.
    """
    if os.environ.get("REGWATCH_FAULT_INJECT", "").strip() != stage:
        return
    if not get_settings().allow_test_providers:
        return
    raise RuntimeError(f"injected {stage} fault (REGWATCH_FAULT_INJECT={stage})")


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
    status: QueryStatusLiteral = "answer"
    # The route reason behind the status (e.g. "multi_form", "no_product",
    # "retrieval"). Surfaced so callers/eval can tell WHY we clarified or
    # refused, not just that we did. Mirrors route_json["reason"].
    reason: str | None = None
    interpretation: str | None = None
    clarify: list[ClarifyOption] = field(default_factory=list)
    # Sibling of `clarify` for the REFUSE family: when we decline (refused=true,
    # citations=[]), `related` surfaces inert "related, not an answer" pointers:
    # distinct product NAMES + their source link only, never passage text/score.
    # It NEVER changes the refusal contract (refused stays true, citations stay
    # []); it is purely additive context the UI renders as re-runnable pills.
    related: list[ClarifyOption] = field(default_factory=list)
    session_id: str | None = None
    turn_id: str | None = None
    # Eval-only ledger passthrough; see RagOutcome.claim_tags.
    claim_tags: tuple[ClaimTag, ...] = ()


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
        "hey",
        "thanks",
        "thank",
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

_FOLLOW_UP_PREFIXES = (
    "what about",
    "how about",
    "and what",
    "also",
    "what does it",
    "does it",
    "what else",
    "can you also",
    # Drill-down openers. These are what a user actually types after reading an
    # analysis, and WITHOUT them the session product is dropped and the turn
    # lands on the no_product refusal -- "why?" carries no pronoun from
    # _FOLLOW_UP_PRONOUNS, so it never matched. That is the literal failure the
    # conversational requirement describes.
    #
    # Safe to widen ONLY because two guards land in the same change:
    #   * ask_core computes the "did you mean"/brand candidates BEFORE the
    #     carry-over, so a question naming a DIFFERENT product breaks the chain
    #     instead of inheriting the session's drug;
    #   * _retrieval_query re-anchors a contentless follow-up on the prior
    #     question, so "why?" is never embedded verbatim.
    # Removing either guard makes this tuple a cross-product leak.
    "why",
    "what should",
    "how do i",
    "how would",
    "how does that",
    "would that",
    "would this",
    "is that",
    "is it",
    "tell me more",
    "what if",
    "explain",
    "go on",
)
_FOLLOW_UP_PRONOUNS = frozenset({"it", "its", "this", "that", "same"})
# Discourse vocabulary that carries conversational intent but NO retrieval
# signal against a corpus of FDA product-specific guidance. Used only by
# _carries_own_topic to decide whether a follow-up needs re-anchoring on the
# prior question before it is embedded. Every word here must be one that would
# never be a useful search term in this corpus -- adding a domain word
# ("dissolution", "bioequivalence") would silently suppress a real query.
_DRILL_DOWN_WORDS = frozenset(
    {
        "why",
        "how",
        "explain",
        "elaborate",
        "expand",
        "more",
        "detail",
        "details",
        "tell",
        "go",
        "again",
        "further",
        "that",
        "this",
        "it",
        "its",
        "should",
        "change",
        "fix",
        "instead",
        "if",
        "then",
        "so",
        "but",
        "ok",
        "okay",
        "yes",
        "mean",
        "means",
        "say",
        "said",
    }
)
_SUMMARY_TERMS = frozenset({"summarize", "summary", "overview", "recap"})
_SCOPE_WARNING_PHRASES = (
    "submission strategy",
    "filing strategy",
    "file the anda",
    "author the anda",
    "write the anda",
    "draft the anda",
    "what should we file",
    "should we file",
    "should we submit",
    "recommend a strategy",
    "regulatory strategy",
    "internal benchmarks",
)
# CLOSED set of "what does this system do" phrases. The bar is deliberately
# high: every phrase must be unmistakably ABOUT the tool's scope, never about a
# regulatory fact. False-negatives are SAFE: a missed meta phrase just falls
# through to the normal grounded path, where facts get cited. False-positives
# are the danger
# (a drug question routed to the uncited meta answer), so the gate also carries
# a named-drug HARD VETO in ask(); these phrases stay tight as defense in depth.
_META_PHRASES = (
    "what do you cover",
    "what products do you cover",
    "what drugs do you cover",
    "what can i ask",
    "what can i ask about",
    "what can you do",
    "what do you watch",
    "what do you monitor",
    "what are you watching",
    "what's on the watchlist",
    "whats on the watchlist",
    "what changed",
    "what's changed",
    "whats changed",
    "what's new",
    "whats new",
)

# Synthesis temperature. A constant, not a setting: determinism is an invariant
# here, not an operator knob. The output cap is the operator knob
# (SYNTHESIZER_MAX_TOKENS) and ask_core reads it ONCE per turn.
_SYNTH_TEMPERATURE = 0.0
# Hard ceiling on a single synthesis call, INDEPENDENT of the setting. Defined
# in config.settings next to the setting it bounds, so the field validator can
# refuse a budget at or above it at boot; re-exported under the private name
# because callers and tests reference qa_mod._SYNTH_MAX_TOKENS_CEILING.
_SYNTH_MAX_TOKENS_CEILING = SYNTH_MAX_TOKENS_CEILING


def _looks_like_follow_up(question: str) -> bool:
    q = question.strip().lower()
    if any(q.startswith(prefix) for prefix in _FOLLOW_UP_PREFIXES):
        return True
    tokens = {t for t in re.split(r"[^a-z0-9]+", q) if t}
    return bool(tokens & _FOLLOW_UP_PRONOUNS) and len(tokens) <= 8


def _is_summary_request(question: str) -> bool:
    tokens = {t for t in re.split(r"[^a-z0-9]+", question.lower()) if t}
    return bool(tokens & _SUMMARY_TERMS)


def _is_scope_warning_request(question: str) -> bool:
    q = question.lower()
    return any(phrase in q for phrase in _SCOPE_WARNING_PHRASES)


def _is_meta_request(question: str) -> bool:
    """True when the question is a closed-set "what does this system do" phrase.

    Phrase-match ONLY. No LLM judges intent (an LLM mis-call would be the exact
    fabrication breach). A True here is necessary but NOT sufficient to route to
    the uncited meta path: ask() additionally vetoes any question that resolves
    to a named in-corpus drug, so "what BE study do you cover for atorvastatin?"
    never reaches _meta.
    """
    q = question.lower()
    return any(phrase in q for phrase in _META_PHRASES)


def _audit_retrieved(passages: list[RetrievedPassage]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": p.chunk_id,
            "score": p.score,
            "doc_id": p.doc_id,
            "version_id": p.version_id,
            "page": p.page,
            "normalized_name": p.normalized_name,
            "short_name": p.short_name,
        }
        for p in passages
    ]


def _enrich_citation_recency(citations: list[Citation]) -> list[Citation]:
    """Resolve each citation's revision date BEFORE the turn is persisted.

    This join used to run only on the response path (api.main._wire_citations),
    so ``recommended_date`` never reached ``citations_json`` and every reopened
    conversation rendered "Revision date not recorded" forever. Running it here
    puts the version-correct date on the domain citation, which is what
    ``_build_patch`` serializes.

    One batched lookup for the whole turn (no N+1), and ``fetch_citation_recency``
    already swallows every exception to an empty index behind the connection's
    statement_timeout -- so a failed or slow lookup yields null dates and an
    answer that still ships.
    """
    if not citations:
        return citations
    recency = fetch_citation_recency(
        sorted({c.version_id for c in citations}),
        sorted({c.doc_id for c in citations}),
    )
    enriched: list[Citation] = []
    for citation in citations:
        resolved = recency.resolve(citation.version_id, citation.doc_id)
        enriched.append(
            replace(
                citation,
                recommended_date=resolved.recommended_date,
                diff_summary=resolved.diff_summary,
            )
        )
    return enriched


def _route_json(
    *,
    filters: dict[str, Any],
    reason: str,
    context_applied: bool,
    response_mode: str,
) -> dict[str, Any]:
    return {
        "route": "psg_scoped_rag",
        "filters": dict(filters),
        "reason": reason,
        "context_applied": context_applied,
        "response_mode": response_mode,
        # The stage-1 search ledger, UNCONDITIONAL: empty means the turn ended
        # before search ran, populated records what ran (mode, scope, profile,
        # k, returned). "Did we search?" is a VALUE, never key presence: the
        # same reason declines on BOTH sides of retrieve() -- multi_form fires
        # at the pre-retrieval form guard and again at the post-retrieval blend
        # backstop -- so presence could not answer it. Callers that do search
        # overwrite this default.
        "retrieval": {},
    }


def _build_patch(
    outcome: RagOutcome,
    *,
    filters: dict[str, Any],
    route_json: dict[str, Any],
) -> SessionPatch:
    """The chat-history mutations this turn implies -- computed, never applied.

    Pure: mirrors what the shell's assistant-message write needs, with the
    audit_id deliberately absent (the audit row does not exist yet; the shell
    injects it in _apply_session_patch after logging).
    """
    return SessionPatch(
        session_id=outcome.session_id,
        turn_id=outcome.turn_id,
        content=outcome.answer,
        status=outcome.status,
        model_name=outcome.model_name,
        reason=outcome.reason,
        interpretation=outcome.interpretation,
        filters=dict(filters),
        citations=[asdict(c) for c in outcome.citations],
        clarify=[asdict(o) for o in outcome.clarify],
        related=[asdict(o) for o in outcome.related],
        metadata={"retrieved": outcome.retrieved, "route": route_json},
        update_filters=bool(
            outcome.status in {"answer", "summary", "clarify"} and filters.get("normalized_name")
        ),
    )


def _apply_session_patch(patch: SessionPatch, *, audit_id: int) -> None:
    if patch.session_id and patch.turn_id:
        # Best-effort chat-history write: the audit row (INV-6) is already
        # committed by this point, so a failure here must not 500 an
        # already-audited turn. Example: the degraded session_id=turn_id
        # fallback has no chat_session row, so the assistant FK insert fails.
        try:
            record_message(
                session_id=patch.session_id,
                turn_id=patch.turn_id,
                role="assistant",
                content=patch.content,
                status=patch.status,
                model_name=patch.model_name,
                audit_id=audit_id,
                reason=patch.reason,
                interpretation=patch.interpretation,
                filters=patch.filters,
                citations=patch.citations,
                clarify=patch.clarify,
                related=patch.related,
                metadata=patch.metadata,
            )
            if patch.update_filters:
                update_session_filters(patch.session_id, patch.filters)
        except Exception:
            log.warning("assistant_record_message_failed", exc_info=True)


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


def _carries_own_topic(question: str, normalized_name: str | None) -> bool:
    """True when the question has a term worth embedding on its own.

    Deliberately NOT ``not _looks_vague(...)``. The vague gate asks "is there
    anything here besides the drug name and filler", and "why" is neither, so
    it reads as a topic. For RETRIEVAL the question is different: "why" is pure
    discourse and embeds to nothing useful. _DRILL_DOWN_WORDS is that
    difference -- meta/discourse vocabulary that is never an FDA-guidance
    retrieval term in this corpus.

    Conservative on purpose: any survivor counts as a topic, so "what about
    dissolution?" keeps its own embedding and today's working follow-ups are
    untouched. The cost is that some phrasings ("what if the study fails?")
    are not re-anchored; those behave exactly as they do today.
    """
    drug_tokens = (
        {t for t in re.split(r"[^a-z0-9]+", normalized_name.lower()) if t}
        if normalized_name
        else set()
    )
    return any(
        t
        for t in re.split(r"[^a-z0-9]+", question.lower())
        if t and t not in drug_tokens and t not in _FILLER and t not in _DRILL_DOWN_WORDS
    )


def _retrieval_query(
    question: str,
    *,
    normalized_name: str | None,
    prior_turns: list[PriorTurn],
) -> str:
    """The text to EMBED for this turn -- not necessarily the user's words.

    A drill-down follow-up ("why?", "tell me more") carries no topical signal.
    Embedded verbatim it scores near-zero against every passage, so the turn
    dies on the ``low_top_score`` refusal even though the session context makes
    the intent obvious. Re-anchor it on the most recent prior QUESTION so the
    vector search sees the subject the user is still asking about.

    RETRIEVAL ONLY. The synthesizer still receives the user's literal question
    plus the conversation block, so the answer addresses what was actually
    asked. INV-1 is untouched either way: ``admit_turn`` validates every
    citation against THIS turn's passages, so how a passage was FOUND cannot
    make an unsupported claim citable.

    Falls through to the raw question whenever the rewrite would be a guess --
    no product pinned, the question has a topic of its own, or there is no
    prior turn to anchor on.
    """
    if not _looks_like_follow_up(question) or _carries_own_topic(question, normalized_name):
        return question
    prior = next((t.question.strip() for t in reversed(prior_turns) if t.question.strip()), "")
    if not prior:
        return question
    # Same strip/cap as _format_recent: a stale "[PSG, p.4]" in the prior
    # question is noise in an embedding, and an unbounded prior question would
    # let one long turn dominate the vector.
    return f"{strip_all_citations(prior).strip()[:400]} {question}".strip()


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
    return f"You're asking about {normalized_name.title()}. {have} — what would you like to know?"


def build_options(normalized_name: str) -> list[ClarifyOption]:
    """Plain-language things we can actually answer for a resolved product.

    These are QUESTION TEMPLATES that re-run retrieval. They do not read the
    (possibly empty) BeRequirement table, so they work on the full catalog.
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


def _related_from_passages(passages: list[RetrievedPassage]) -> list[ClarifyOption]:
    """ "Related, not an answer" pointers from the sub-threshold passages in hand.

    Surfaces DISTINCT product NAMES ONLY, never the passage text or score
    (chunk text would read as quasi-evidence on a refusal). Deduped by product
    name, first occurrence wins (retrieval order = best match first). Each
    option re-runs as a name-scoped query, so it renders as an inert,
    re-runnable pill, never a citation chip. Refused/citations are untouched.
    Filters carry retrieval constraints only, so no display values (source_url)
    belong here; the API boundary would strip them from an echo anyway.
    """
    options: list[ClarifyOption] = []
    seen: set[str] = set()
    for p in passages:
        name = (p.normalized_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        options.append(ClarifyOption(name.title(), name, {"normalized_name": name}))
    return options


def _options_from_names(names: list[str]) -> list[ClarifyOption]:
    """Re-runnable product pills from a list of product names.

    THE one rule for how a bare product name becomes a clickable option --
    clarify choices (ambiguous / did-you-mean / brand / mixed-products) and the
    decline family's inert "related, not an answer" pointers all share it, so
    the two pill kinds can never render or behave differently for the same
    name. No DB hit; an empty list yields ``[]`` -- never raises.
    """
    return [ClarifyOption(name.title(), name, {"normalized_name": name}) for name in names]


def _combo_label(normalized_name: str, dosage_form: str, route: str) -> str:
    """Human-readable combo label, e.g. ``Estradiol — Gel, Metered (Transdermal)``."""
    return f"{normalized_name.title()} — {dosage_form} ({route})"


def build_form_options(
    normalized_name: str, combos: list[tuple[str, str]], question: str
) -> list[ClarifyOption]:
    """One clickable option per (dosage_form, route) combo of a multi-form drug.

    Each option re-runs the SAME question (so the user's intent survives the
    extra hop) but pins ``dosage_form`` + ``route`` alongside ``normalized_name``
    so retrieval is constrained to a single form, which is what stops a
    wrong-form PSG being cited. Filters round-trip verbatim through the API/UI.
    """
    options: list[ClarifyOption] = []
    for dosage_form, route in combos:
        options.append(
            ClarifyOption(
                _combo_label(normalized_name, dosage_form, route),
                question,
                {
                    "normalized_name": normalized_name,
                    "dosage_form": dosage_form,
                    "route": route,
                },
            )
        )
    return options


def _form_match_tokens(value: str) -> set[str]:
    """Significant form/route tokens for whole-word matching (drop short connectors)."""
    return {t for t in re.split(r"[^a-z0-9]+", value.lower()) if len(t) > 2}


def _combo_from_question(question: str, combos: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Pin a single (dosage_form, route) combo when the question names it unambiguously.

    Scores each combo by how many of its significant dosage_form/route tokens appear
    as whole words in the question; returns the uniquely best-matching combo (score
    > 0 and strictly ahead of the runner-up), else None. So "albuterol sulfate
    inhalation aerosol" pins (Aerosol, Metered)/(Inhalation) instead of paying a
    pointless clarify hop, while a form-silent or ambiguous question still clarifies.

    Two corrections keep this honest on the oral-tablet mass (68% of the catalog):
      * _FILLER is stripped from the question tokens first. Otherwise the 3-letter
        stopword "for" (which survives the len>2 cut in _form_match_tokens) collides
        with real catalog forms like "Tablet, For Suspension" / "For Solution" and
        SILENTLY pins the wrong form: a wrong-form citation, the worst INV-1 outcome.
        "for"/"of"/"about" are already in _FILLER; no real form word is.
      * Ties on raw match count are broken toward the combo the question covers most
        COMPLETELY (fewest of the combo's own tokens left unmentioned), so a plain
        "tablet" pins (Tablet) over its (Tablet, Extended Release) sibling instead of
        a pointless clarify, while "extended release tablet" still pins the ER variant
        and a token like "tablet" that fits ER and ODT equally still clarifies.
      * The completeness tie-break only applies when the question matched at least
        one DOSAGE-FORM token. A match-count tie carried by shared ROUTE tokens
        alone ("albuterol sulfate inhalation" against the aerosol AND the solution
        combo) names no form, so breaking it by token count would silently pin an
        arbitrary form -- clarify instead. A route mention that is unique to one
        combo still pins (no tie to break).
    """
    q_tokens = {t for t in re.split(r"[^a-z0-9]+", question.lower()) if t and t not in _FILLER}

    def _score(form: str, route: str) -> tuple[tuple[int, int], int]:
        form_tokens = _form_match_tokens(form)
        combo_tokens = form_tokens | _form_match_tokens(route)
        matched = len(combo_tokens & q_tokens)
        # primary: more matched tokens; secondary: fewer of the combo's own tokens
        # left uncovered (negated so "more complete" sorts first). The form-token
        # match count rides ALONGSIDE the sort key (never inside it -- an existing
        # full-tuple tie must stay a tie) for the route-only guard below.
        return (matched, -(len(combo_tokens) - matched)), len(form_tokens & q_tokens)

    scored = sorted(
        ((*_score(form, route), (form, route)) for form, route in combos),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, best_form_matched, best_combo = scored[0]
    if best_score[0] == 0:
        return None  # the question named no form at all, so clarify
    if len(scored) > 1 and scored[1][0] == best_score:
        return None  # two combos fit equally well (match AND completeness)
    if best_form_matched == 0 and len(scored) > 1 and scored[1][0][0] == best_score[0]:
        # The win came ONLY from the completeness tie-break over shared route
        # tokens -- the question named no dosage form, so a pin here would be the
        # silent wrong-form pin this function exists to prevent. Clarify.
        return None
    return best_combo


def _format_passages(passages: list[RetrievedPassage]) -> str:
    blocks: list[str] = []
    for p in passages:
        section = f" ({p.section_path})" if p.section_path else ""
        blocks.append(f"[{p.short_name}, p.{p.page}]{section}\n{p.text.strip()}\n")
    return "\n---\n".join(blocks)


def _format_passages_numbered(passages: list[RetrievedPassage]) -> str:
    """The v6 prose variant: a [n] label prefixes the same pair header.

    The [n] label is what the model cites; the [SHORT_NAME, p.N] pair header
    stays because the prose parser's pair-echo collision handling and the echo
    provider's discriminating scrape both key on it, and an operator reading a
    raw prompt still sees the canonical citation identity next to each number.
    """
    blocks: list[str] = []
    for position, p in enumerate(passages, start=1):
        section = f" ({p.section_path})" if p.section_path else ""
        blocks.append(f"[{position}] [{p.short_name}, p.{p.page}]{section}\n{p.text.strip()}\n")
    return "\n---\n".join(blocks)


# Bare numeric markers ("[3]", "[1, 2]") in conversation memory. Stripped by
# _format_recent ALONGSIDE the pair-form strip: under v6 prose a stale [3] in a
# prior answer would read as a live pointer into THIS turn's passage numbering.
# Unconditional and plain-deletion on purpose -- v5 history contains no numeric
# markers, so the sub is a byte-level no-op there and the v5 prompt (and its
# eval) stays byte-identical.
_NUMERIC_MARKER_RE = re.compile(r"\[\s*\d+\s*(?:,\s*\d+\s*)*\]")


def _format_recent(turns: list[PriorTurn]) -> str:
    """Render prior turns as a compact, citation-free conversation context block.

    Citations are stripped so the model cannot see, and therefore cannot
    parrot, a stale ``[PSG, p.N]`` whose page may not be in THIS turn's
    passages, and
    each side is capped so the current passages stay dominant in the window.
    Reference-only, never evidence (INV-1): the system prompt forbids treating it
    as a source, and _validate_citations accepts only markers grounded in this
    turn's passages regardless.
    """
    lines: list[str] = []
    for t in turns:
        # Strip markers from BOTH sides: a citation-shaped token in a prior
        # question is context too, never a source the model may reuse this turn.
        q = _NUMERIC_MARKER_RE.sub("", strip_all_citations(t.question)).strip()[:400]
        # Stored answers end with a "Sources:" trailer. Current entries are
        # bracketed, while legacy entries were not; drop the whole trailer before
        # the bracket strip (shared with eval/metrics.faithfulness) so neither
        # form can become a stale re-citable pointer in conversation memory.
        answer_prose = strip_sources_trailer(t.answer)
        a = _NUMERIC_MARKER_RE.sub("", strip_all_citations(answer_prose)).strip()[:600]
        if not q and not a:
            continue
        lines.append(f"User: {q}\nAssistant: {a}")
    return "\n\n".join(lines)


def _complete_structured(
    provider: LLMProvider,
    messages: list[LLMMessage],
    *,
    max_tokens: int,
    response_format: str | None = "json",
    telemetry: dict[str, Any] | None = None,
) -> LLMResponse:
    """One buffered completion, with a single 2x truncation retry.

    ``response_format`` defaults to "json" (every legacy caller); the v6 prose
    synthesizer passes None so no json_object mode -- and no appended user json
    directive -- reaches the wire. Everything else (buffering, D1-first
    re-raise, the bounded truncation retry) is format-independent.

    Buffered, never ``provider.stream()``: stream() takes no response_format on
    the Protocol or in any implementation, ``_buffered_stream`` drops it, and
    the Databricks stream fallback re-issues through ``_buffered_stream`` -- so
    a schema-rejecting endpoint would silently hand unstructured PROSE back to a
    structured caller. The user-visible "typing" effect is preserved by replaying
    the RENDERED answer after the audit write (see ``_persist_turn``).

    D1ResidencyError is re-raised FIRST: a residency violation must fail the
    turn loudly, never be retried against the very endpoint the guard fences off
    and never degrade into a parse failure.
    """
    capped = min(max_tokens, _SYNTH_MAX_TOKENS_CEILING)
    if telemetry is not None:
        telemetry["first_budget"] = capped
    try:
        return provider.complete(
            messages,
            temperature=_SYNTH_TEMPERATURE,
            max_tokens=capped,
            response_format=response_format,
        )
    except D1ResidencyError:
        raise
    except RuntimeError as exc:
        # RuntimeError is the provider layer's "the call returned, but the
        # payload is unusable" signal, and truncation is its dominant cause
        # (OpenAI status="incomplete", Databricks finish_reason="length"). It is
        # not EXCLUSIVELY truncation -- OpenAI status="failed", Databricks
        # finish_reason="content_filter" and "no choices" raise the same type --
        # so the retry can cost one extra call on a non-truncation fault. That
        # is bounded to one and never changes the outcome, and narrowing the
        # predicate would couple this module to provider message text; do it
        # with a typed exception in llm.py, not a substring match. Transport
        # faults (429/5xx/timeouts) are openai.APIError, NOT RuntimeError, so
        # they skip this branch entirely and land on the audited
        # provider_error path on the first failure.
        # Compute the retry budget FIRST and let it speak for itself. The old
        # form tested `capped >= CEILING` and left the reader to work out that
        # the doubling below could not then produce anything larger. Identical
        # behaviour -- `retry_budget <= capped` iff `capped >= CEILING`, since
        # capped is already min()'d -- but the condition now states the actual
        # reason: there is no bigger budget to escalate to, and re-issuing a
        # byte-identical request at temperature 0.0 would only burn a call.
        retry_budget = min(capped * 2, _SYNTH_MAX_TOKENS_CEILING)
        if retry_budget <= capped:
            raise
        if telemetry is not None:
            telemetry["synthesis_retried"] = True
            telemetry["retry_budget"] = retry_budget
        log.warning(
            "qa_synthesis_truncation_retry",
            old=capped,
            new=retry_budget,
            error=str(exc)[:200],
        )
        return provider.complete(
            messages,
            temperature=_SYNTH_TEMPERATURE,
            max_tokens=retry_budget,
            response_format=response_format,
        )


def _stream_structured(
    provider: LLMProvider,
    messages: list[LLMMessage],
    *,
    max_tokens: int,
    telemetry: dict[str, Any] | None = None,
    on_delta: Callable[[str], None],
    on_reset: Callable[[], None],
) -> LLMResponse:
    """Streaming twin of ``_complete_structured`` -- prose mode only.

    Forwards scrubbed deltas to ``on_delta`` while the model writes, then
    returns the terminal LLMResponse; the parse/admit gate downstream always
    operates on that COMPLETE text, exactly as on the buffered path. The
    sentinel hold lives HERE, provider-agnostically (Echo streams too): no
    delta is forwarded while the accumulated text is still a prefix of
    PROSE_NO_EVIDENCE_SENTINEL, so a refusal never paints. Truncation keeps
    the same one-2x-retry policy; because attempt 1's deltas may already be
    on the wire, the retry emits ``on_reset`` first (the client discards the
    partial draft) and re-streams. D1ResidencyError re-raises first, exactly
    like the buffered twin.
    """
    from regwatch.generate.prose_turn import PROSE_NO_EVIDENCE_SENTINEL

    capped = min(max_tokens, _SYNTH_MAX_TOKENS_CEILING)
    if telemetry is not None:
        telemetry["first_budget"] = capped

    def _attempt(budget: int) -> tuple[LLMResponse | None, bool]:
        """(terminal response | None, any_delta_forwarded)."""
        held = ""
        holding = True
        forwarded = False

        def _forward(text: str) -> None:
            nonlocal forwarded
            if not text:
                return
            forwarded = True
            try:
                on_delta(text)
            except Exception:  # broad: a draft sink is cosmetic, never fatal
                log.debug("on_draft_failed", exc_info=True)

        response: LLMResponse | None = None
        for chunk in provider.stream(messages, temperature=_SYNTH_TEMPERATURE, max_tokens=budget):
            if chunk.reset:
                held, holding = "", True
                if forwarded:
                    try:
                        on_reset()
                    except Exception:
                        log.debug("on_draft_reset_failed", exc_info=True)
                continue
            if chunk.done:
                response = chunk.response
                break
            if holding:
                held += chunk.delta
                if PROSE_NO_EVIDENCE_SENTINEL.startswith(held):
                    continue  # still a possible refusal prefix - keep holding
                holding = False
                _forward(held)
                held = ""
                continue
            _forward(chunk.delta)
        return response, forwarded

    try:
        response, _ = _attempt(capped)
    except D1ResidencyError:
        raise
    except RuntimeError as exc:
        retry_budget = min(capped * 2, _SYNTH_MAX_TOKENS_CEILING)
        if retry_budget <= capped:
            raise
        if telemetry is not None:
            telemetry["synthesis_retried"] = True
            telemetry["retry_budget"] = retry_budget
        log.warning(
            "qa_synthesis_truncation_retry", old=capped, new=retry_budget, error=str(exc)[:200]
        )
        try:
            on_reset()
        except Exception:
            log.debug("on_draft_reset_failed", exc_info=True)
        response, _ = _attempt(retry_budget)
    if response is None:
        raise RuntimeError("provider stream ended without a terminal response chunk")
    return response


def _gate_failure_class(detail: str) -> str:
    """Bucket a gate parse failure into a cause an operator can act on.

    ``malformed_structure`` fuses four unrelated faults -- the model exceeded a
    schema cap, it emitted invalid JSON, it emitted nothing extractable, or it
    broke the schema some other way -- and the remedy differs for each (raise a
    cap / change decoding / check the endpoint / fix the prompt). Today they are
    indistinguishable in the DB, so the rate is uninterpretable.

    Substring matching is acceptable ONLY because every input string is
    produced by this repo's own turn_gate (the three GateFailure sites) or by
    pydantic's ValidationError json. It must never be pointed at provider text.
    """
    d = detail.lower()
    if "json decode failed" in d:
        return "json_decode"
    if "empty response after extraction" in d:
        return "empty_extract"
    # Match pydantic's error type EXACTLY. A plain `"too_long" in d` also
    # matches "string_too_long", fusing "the model wrote 21 claims" with "the
    # model wrote a 401-char claim" -- different faults with different fixes
    # (raise the list cap vs. tighten the one-sentence instruction).
    if '"type":"too_long"' in d:
        return "list_too_long"
    if '"type":"string_too_long"' in d:
        return "text_too_long"
    return "schema_other"


def _gate_log_fields(admitted: AdmittedTurn) -> dict[str, Any]:
    """Operator counters for one gate decision -- no claim text, no citations.

    The full per-claim record (text prefix, cites, drop reason, materiality
    word) is persisted in route_json["turn"]; this line exists so the drop rate
    is greppable without a DB query.
    """
    counts: dict[str, int] = {}
    for claim in admitted.dropped:
        counts[claim.reason] = counts.get(claim.reason, 0) + 1
    return {
        "verdict": admitted.verdict,
        "emitted": admitted.emitted,
        "admitted": len(admitted.admitted),
        "dropped": len(admitted.dropped),
        "material_word": admitted.material_word,
        "drop_reasons": counts,
    }


def _replay_chunks(text: str) -> list[str]:
    """Split a rendered answer into ~60-char whitespace-boundary chunks.

    Whitespace boundaries only: splitting mid-token could tear a citation
    marker across two frames, and a half-marker is exactly the shape a client
    would render as literal prose.
    """
    chunks: list[str] = []
    current = ""
    for token in re.split(r"(\s+)", text):
        if not token:
            continue
        current += token
        if len(current) >= _REPLAY_CHUNK_CHARS and not token.isspace():
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _usage_fields(model_name: str, usage: LLMUsage | None) -> dict[str, Any]:
    """log_query kwargs for the turn's synthesizer or guidance usage (H3).

    None (no LLM call happened) keeps all three columns NULL; an unpriced
    model keeps cost_usd NULL while still recording the token counts.
    """
    if usage is None:
        return {}
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": estimate_cost_usd(model_name, usage),
    }


# Fixed, non-LLM copy for the status="error" refusal family (provider transport
# failure, catalog read failure, audit write failure). One literal so every
# degrade path stays in sync with the tests that assert on it.
# The corrective turn appended for the ONE bounds repair (issue #183). Phrased
# as a normal follow-up rather than an error report: the model is being asked
# to say the same thing more concisely, not told it violated a schema. No
# character count is quoted -- a number invites the model to pad up to it.
#
# Appended AFTER the original messages, so attempt 1's prompt bytes (and its
# prompt fingerprint) are untouched. Only a repaired turn carries the extra
# pair, and synth_telemetry stamps that it happened.
_BOUNDS_REPAIR_TURN: dict[str, tuple[LLMMessage, ...]] = {
    "sentence_too_long": (
        LLMMessage(
            role="user",
            content=(
                "One of those sentences ran far too long. Say the same thing "
                "again, keeping every fact and every [n] marker exactly as you "
                "had them, but break it into ordinary-length sentences."
            ),
        ),
    ),
    "answer_too_long": (
        LLMMessage(
            role="user",
            content=(
                "That answer ran far too long overall. Give the same answer "
                "again, keeping every fact and every [n] marker exactly as you "
                "had them, but say it more concisely and without repeating "
                "yourself."
            ),
        ),
    ),
}

# Audit-only reason codes. Greppable, and deliberately distinct from
# malformed_structure so the ~12% prod parse-failure baseline stays comparable
# and a bounds breach never hides inside it.
_BOUNDS_REASON = {
    "sentence_too_long": "oversize_sentence",
    "answer_too_long": "oversize_answer",
}

_SERVICE_UNAVAILABLE_TEXT = (
    "The answer service is temporarily unavailable. Your question was "
    "not answered — please try again in a moment."
)

# Fixed, non-LLM copy for the conversational outcomes. Server-owned like every
# other non-answer string here: the model never writes display prose, and a
# greeting must not cost a model call at all.
CONVERSE_GREETING_TEXT = "Hey! What can I help you with today?"
NEED_PRODUCT_GUIDANCE_TEXT = "Sure — which product are you asking about?"


def _log_query_or_skip(**kwargs: Any) -> int:
    """``log_query`` with a DEFINED failure: -1 when the audit write fails.

    Used only by the no-LLM-content terminal paths (``_refuse``/``_clarify``/
    ``_meta``): their payload is fixed copy or system-state text with zero
    citations, so returning it without an audit row beats a naked, unaudited
    500 that the stream-fallback client would re-run into the same down DB. The skip is logged and Sentry-captured; -1
    never collides with a real QueryLog id.
    """
    try:
        return log_query(**kwargs)
    except Exception as exc:
        log.warning("qa_audit_write_failed", error=str(exc), error_type=type(exc).__name__)
        capture_exception(exc)
        return -1


def _latency_ms(t0: float | None) -> int | None:
    """Whole-millisecond turn wall time, or None. Go's ``latencyMs`` twin.

    ``perf_counter`` is monotonic, so only a missing start stamp yields None.
    None, never 0: a percentile over a column where "unknown" and "instant"
    both read 0 understates the provider-cutover gates that consume it. The
    clamp is a column-width guard (int4), not a reachable path.
    """
    if t0 is None:
        return None
    return min(int((perf_counter() - t0) * 1000), 2**31 - 1)


def _persist_turn(
    outcome: RagOutcome,
    audit: AuditPayload,
    patch: SessionPatch,
    t0: float | None = None,
    on_token: Callable[[str], None] | None = None,
) -> QAResult:
    """The shell's write half of a turn: audit row FIRST, then the chat history.

    Everything user-visible is decided by the (pure) core; this function only
    performs the writes the core described and injects the audit_id they share.

    ``t0`` is the shell's turn clock. Latency is stamped HERE rather than
    supplied by the core because the core is stateless and cannot see transport
    time. Go's ``auditParams`` makes the same split.

    ``on_token`` replays the RENDERED, gated answer AFTER the audit write
    succeeds, and only on an answer/summary turn. This is deliberately not where
    it used to live: streaming provisional model tokens from inside the core
    meant a user could read text that the gate later retracted, and it meant a
    complete answer could reach the reader with no audit row anywhere (INV-6).
    The cost is time-to-first-token; the compensation is that no byte a user
    sees is ever un-audited or un-gated.
    """
    log_kwargs = {**audit.log_kwargs(), "latency_ms": _latency_ms(t0)}
    if audit.allow_skip:
        audit_id = _log_query_or_skip(**log_kwargs)
    else:
        try:
            audit_id = log_query(**log_kwargs)
        except Exception as exc:
            # No-audit-no-answer (INV-6): a validated answer with no audit row
            # is never returned -- but the failure must be DEFINED, not a naked
            # 500 that the stream-fallback client re-runs (a second paid
            # synthesis) into the same down DB. Degrade to the core-supplied
            # fixed-copy status="error" refusal turn, whose own audit is
            # re-attempted and skipped (flagged) if the DB is still down.
            log.warning(
                "qa_answer_audit_write_failed", error=str(exc), error_type=type(exc).__name__
            )
            capture_exception(exc)
            if audit.failure_fallback is None:  # defensive: strict payloads carry one
                raise
            fb_outcome, fb_audit, fb_patch = audit.failure_fallback
            # Same t0: the fallback row records how long THIS turn took, not
            # how long the retry after a failed audit write took.
            return _persist_turn(fb_outcome, fb_audit, fb_patch, t0, on_token)
    # Terminal-decline log lines, emitted after the audit write exactly as the
    # pre-split _refuse/_clarify/_meta did (each status maps to one maker, so
    # the event names cannot drift). The answer/summary path never logged here.
    if outcome.status == "clarify":
        log.info(
            "qa_clarify", reason=outcome.reason, audit_id=audit_id, options=len(outcome.clarify)
        )
    elif outcome.status == "meta":
        log.info("qa_meta", audit_id=audit_id)
    elif outcome.refused:
        log.info("qa_refused", reason=outcome.reason, audit_id=audit_id)
    # The "typing" effect, rebuilt on the safe side of the write. The status
    # filter is the whole guard: a decline, a clarify or an error replays
    # NOTHING, so a retracted draft can never be painted for a beat and then
    # vanish. Best-effort, exactly like on_progress -- a failing sink must never
    # break a turn that is already audited and already answered.
    if on_token is not None and outcome.status in ("answer", "summary"):
        for chunk in _replay_chunks(outcome.answer):
            try:
                on_token(chunk)
            except Exception:  # broad: a token sink is cosmetic, never fatal
                # Stop replaying rather than hammer a sink that just failed: the
                # turn is already audited and the authoritative answer still
                # rides the terminal result frame.
                log.debug("on_token_failed", exc_info=True)
                break
    result = QAResult(
        answer=outcome.answer,
        citations=outcome.citations,
        refused=outcome.refused,
        model_name=outcome.model_name,
        audit_id=audit_id,
        retrieved=outcome.retrieved,
        status=outcome.status,
        reason=outcome.reason,
        interpretation=outcome.interpretation,
        clarify=outcome.clarify,
        related=outcome.related,
        session_id=outcome.session_id,
        turn_id=outcome.turn_id,
        claim_tags=outcome.claim_tags,
    )
    _apply_session_patch(patch, audit_id=audit_id)
    return result


def _refuse(
    *,
    question: str,
    passages: list[RetrievedPassage],
    reason: str,
    model_name: str,
    session_id: str,
    turn_id: str,
    user_id: str | None,
    route_json: dict[str, Any],
    status: QueryStatusLiteral = "refused",
    answer_text: str | None = None,
    usage: LLMUsage | None = None,
    related: list[ClarifyOption] | None = None,
) -> tuple[RagOutcome, AuditPayload]:
    s = get_settings()
    answer = answer_text or s.refusal_text
    audited = _audit_retrieved(passages)
    audit = AuditPayload(
        mode="qa",
        query_text=question,
        retrieved=audited,
        answer_text=answer,
        citations=[],
        refused=True,
        model_name=model_name,
        session_id=session_id,
        turn_id=turn_id,
        user_id=user_id,
        status=status,
        route_json=route_json,
        **_usage_fields(model_name, usage),
    )
    outcome = RagOutcome(
        answer=answer,
        citations=[],
        refused=True,
        model_name=model_name,
        retrieved=audited,
        status=status,
        reason=reason,
        related=related or [],
        session_id=session_id,
        turn_id=turn_id,
    )
    return outcome, audit


def _clarify(
    *,
    question: str,
    reason: str,
    model_name: str,
    interpretation: str,
    options: list[ClarifyOption],
    session_id: str,
    turn_id: str,
    user_id: str | None,
    route_json: dict[str, Any],
    usage: LLMUsage | None = None,
    related: list[ClarifyOption] | None = None,
    passages: list[RetrievedPassage] | None = None,
) -> tuple[RagOutcome, AuditPayload]:
    """Guide instead of guess: we know the product (or a near-match) but need
    direction. Carries ZERO citations (never fabricates) and describes one audit
    row (INV-6), exactly like ``_refuse``.

    ``passages`` is set only by the POST-retrieval defense-in-depth clarifies
    (mixed_products / multi_form backstop) so the audit row keeps the retrieved
    evidence that tripped the guard -- exactly the turns where forensics matter.
    Pre-retrieval clarifies leave it None (nothing was retrieved)."""
    audited = _audit_retrieved(passages or [])
    audit = AuditPayload(
        mode="qa",
        query_text=question,
        retrieved=audited,
        answer_text=interpretation,
        citations=[],
        refused=False,
        model_name=model_name,
        session_id=session_id,
        turn_id=turn_id,
        user_id=user_id,
        status="clarify",
        route_json=route_json,
        **_usage_fields(model_name, usage),
    )
    outcome = RagOutcome(
        answer=interpretation,
        citations=[],
        refused=False,
        model_name=model_name,
        retrieved=audited,
        status="clarify",
        reason=reason,
        interpretation=interpretation,
        clarify=options,
        related=related or [],
        session_id=session_id,
        turn_id=turn_id,
    )
    return outcome, audit


def _scope_warning(
    *,
    question: str,
    reason: str,
    model_name: str,
    session_id: str,
    turn_id: str,
    user_id: str | None,
    route_json: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> tuple[RagOutcome, AuditPayload]:
    # The decline itself never changes (INV-3: we never author the filing
    # decision). But if the question is ABOUT a real product we can name the
    # in-scope, citable sub-questions and hand back re-runnable pointers so the
    # user has a next step instead of a dead end.
    generic = (
        "I can help summarize and answer questions from FDA sources, but I cannot "
        "author submission strategy, recommend what to file, or make a regulatory "
        "judgment. If you name the product and source area, I can look up the FDA "
        "evidence and cite what the records say."
    )
    nm = (filters or {}).get("normalized_name")
    if not nm:
        # Resolution hits the vector store, so it can raise/time out. A resolver
        # failure must NOT break the refusal, so fall back to the generic decline.
        try:
            r = resolve_product(question)
            if r.status == "resolved" and r.normalized_name:
                nm = r.normalized_name
        except Exception:
            log.warning("scope_warning_resolve_failed", exc_info=True)
            nm = None

    if nm:
        options = build_options(str(nm))
        product = str(nm).title()
        answer = (
            f"I can't author submission strategy, recommend what to file, or make "
            f"a regulatory judgment for {product}. What I CAN do is cite the FDA "
            f"record: the recommended bioequivalence (BE) study design, the "
            f"dissolution method, and the strengths and dosage forms the guidance "
            f"covers. Ask me any of those and I'll quote the source."
        )
        return _refuse(
            question=question,
            passages=[],
            reason=reason,
            model_name=model_name,
            session_id=session_id,
            turn_id=turn_id,
            user_id=user_id,
            route_json=route_json,
            status="scope_warning",
            answer_text=answer,
            related=options,
        )

    return _refuse(
        question=question,
        passages=[],
        reason=reason,
        model_name=model_name,
        session_id=session_id,
        turn_id=turn_id,
        user_id=user_id,
        route_json=route_json,
        status="scope_warning",
        answer_text=generic,
    )


# A meta phrase whose subject is "what changed / what's new" pulls the recent
# Watch digest into the answer; everything else describes corpus + watchlist.
_META_CHANGE_PHRASES = ("what changed", "changed", "what's new", "whats new", "new")


def _is_change_request(question: str) -> bool:
    q = question.lower()
    return any(phrase in q for phrase in _META_CHANGE_PHRASES)


def _meta_answer_text(question: str) -> str:
    """Assemble a meta answer from VERIFIED SYSTEM STATE ONLY, never an LLM.

    Three independent system facts, each read live and clearly labeled so they
    are NEVER conflated:
      * corpus: the askable PSGs (distinct normalized_name + doc count),
      * watchlist: the products Watch actively monitors (list_watchlist),
      * what changed: the most recent durable alerts (latest_digest_records),
        included only when the question is a "what changed / what's new" phrase.
    Carries no passage text and no citations; it cannot emit a regulatory claim.
    """
    # Corpus: distinct products you can ASK about. ONE aggregate COUNT -- the
    # per-product _doc_count loop was an N+1 (~1.4k sequential round trips per
    # meta question on the full catalog).
    corpus_names = sorted(n for n in distinct_metadata_values("normalized_name") if n)
    corpus_doc_count = count_documents(corpus_names)
    products = "product" if len(corpus_names) == 1 else "products"
    docs = "document" if corpus_doc_count == 1 else "documents"
    sample = ", ".join(n.title() for n in corpus_names[:5])
    more = "" if len(corpus_names) <= 5 else f", and {len(corpus_names) - 5} more"
    corpus_line = (
        f"You can ask me about {len(corpus_names)} {products} in the FDA "
        f"product-specific guidance corpus ({corpus_doc_count} {docs})"
    )
    corpus_line += f": {sample}{more}." if corpus_names else "."

    # Watchlist: products Watch MONITORS. Distinct from the askable corpus above.
    watch_items = list_watchlist()
    watch_names = sorted(
        {
            str(p.get("normalized_name") or p.get("active_ingredient") or "").strip()
            for p in watch_items
        }
        - {""}
    )
    watched = "product" if len(watch_names) == 1 else "products"
    watch_sample = ", ".join(n.title() for n in watch_names[:5])
    watch_more = "" if len(watch_names) <= 5 else f", and {len(watch_names) - 5} more"
    if watch_names:
        watch_line = (
            f"Separately, Watch monitors {len(watch_names)} {watched} for FDA "
            f"guidance changes: {watch_sample}{watch_more}."
        )
    else:
        watch_line = "Watch is not monitoring any products yet."

    lines = [corpus_line, watch_line]

    if _is_change_request(question):
        records = latest_digest_records(limit=5)
        if records:
            # NON-PROSE system facts ONLY: product name + capture date. The
            # alert's `diff_summary`/`rationale` are LLM output or raw PSG passage
            # text (see process/change_detector.summarize_change). That is a
            # regulatory claim, not a system fact, so it must NEVER reach this
            # uncited meta answer (INV-1). Detail lives on the cited Watch feed.
            change_bits = []
            for r in records:
                name = str(r.get("active_ingredient") or "").strip().title() or "a product"
                # captured_at is an ISO timestamp; keep only the date (system
                # bookkeeping, never regulatory prose). Tolerate odd shapes.
                date = str(r.get("captured_at") or "").strip()[:10]
                change_bits.append(f"{name} ({date})" if date else name)
            count = len(records)
            flagged = "change" if count == 1 else "changes"
            lines.append(
                f"Watch flagged {count} recent guidance {flagged}: "
                + "; ".join(change_bits)
                + ". Open the Watch feed for the cited details of each change."
            )
        else:
            lines.append("Watch has not flagged any guidance changes yet.")

    return " ".join(lines)


def _meta(
    *,
    question: str,
    reason: str,
    model_name: str,
    session_id: str,
    turn_id: str,
    user_id: str | None,
    route_json: dict[str, Any],
) -> tuple[RagOutcome, AuditPayload]:
    """Answer a "what does this system do" question from system state only.

    Mirrors ``_scope_warning`` as an application-owned handler: one audit row
    (INV-6), zero citations, and no model-authored prose. The surrounding
    ``_decline`` ceremony still gives the valid turn one bounded guidance call.
    This is NOT a refusal: ``refused`` is False and ``status`` is "meta". The
    answer is assembled in ``_meta_answer_text`` from corpus/watchlist/digest
    facts, so it is structurally citation- and fabrication-incapable; it can
    never carry a regulatory claim.
    """
    answer = _meta_answer_text(question)
    audit = AuditPayload(
        mode="qa",
        query_text=question,
        retrieved=[],
        answer_text=answer,
        citations=[],
        refused=False,
        model_name=model_name,
        session_id=session_id,
        turn_id=turn_id,
        user_id=user_id,
        status="meta",
        route_json=route_json,
    )
    outcome = RagOutcome(
        answer=answer,
        citations=[],
        refused=False,
        model_name=model_name,
        retrieved=[],
        status="meta",
        reason=reason,
        session_id=session_id,
        turn_id=turn_id,
    )
    return outcome, audit


def _converse(
    *,
    question: str,
    reason: str,
    model_name: str,
    session_id: str,
    turn_id: str,
    user_id: str | None,
    route_json: dict[str, Any],
) -> tuple[RagOutcome, AuditPayload]:
    """Answer a social turn as a person would, with no machinery behind it.

    A greeting is not a failed product lookup. It reaches no resolver, no
    retrieval and no model: the copy is fixed and server-owned, so this handler
    is citation- and fabrication-incapable by construction, exactly like
    ``_meta`` -- whose status it deliberately shares. ``status="meta"`` keeps
    the seven-value ``QueryStatusLiteral`` (and the Go proxy, the SSE grammar,
    the persisted rows and the frontend allowlist that mirror it) untouched,
    and it is the status a promoted ``mode=converse`` turn will land on too.

    ``refused`` is False, so the frontend renders plain prose with no declined
    register, no diagnostic reason line and no "should this have been answered?"
    prompt.
    """
    audit = AuditPayload(
        mode="qa",
        query_text=question,
        retrieved=[],
        answer_text=CONVERSE_GREETING_TEXT,
        citations=[],
        refused=False,
        model_name=model_name,
        session_id=session_id,
        turn_id=turn_id,
        user_id=user_id,
        status="meta",
        route_json=route_json,
    )
    outcome = RagOutcome(
        answer=CONVERSE_GREETING_TEXT,
        citations=[],
        refused=False,
        model_name=model_name,
        retrieved=[],
        status="meta",
        reason=reason,
        session_id=session_id,
        turn_id=turn_id,
    )
    return outcome, audit


@dataclass
class TurnState:
    """Mid-flow mutable state of one ``ask_core`` turn, made explicit.

    One instance per turn, created by ``ask_core`` and threaded through the
    stage functions below; each stage mutates it in place and returns it (or
    a terminal decline triple). The ``_decline`` ceremony reads
    ``active_filters``/``context_applied`` from here at CALL time -- before
    the extraction these were loose closure variables, so that
    read-at-call-time contract was invisible in any signature.
    """

    active_filters: dict[str, Any]
    context_applied: bool = False
    resolved_by_name: bool = False
    response_mode: QueryStatusLiteral = "answer"
    # Filled by _retrieve_and_group for _synthesize_and_admit: every retrieved
    # row (the audit trail), the individually above-threshold subset (all the
    # synthesizer may see), and the retrieval route_json the answer path's
    # audit row carries.
    passages: list[RetrievedPassage] = field(default_factory=list)
    evidence_passages: list[RetrievedPassage] = field(default_factory=list)
    route_json: dict[str, Any] = field(default_factory=dict)
    # Populated once stage-1 search runs; read by _decline so a turn that
    # declines AFTER retrieving still records which retrieval mode ran.
    retrieval_block: dict[str, Any] = field(default_factory=dict)
    # PR11b observation only. The model result cannot steer any field above;
    # its finalized JSON is copied into the audit route immediately before the
    # existing path returns or retrieves.
    route_shadow: RouteShadowObservation | None = None
    route_shadow_audit: dict[str, Any] | None = None
    # PR12, live mode only: the compiled scope that actually steered this
    # turn's pre-retrieval path (set only from _resolve_and_carry_over's
    # no-product branch), and the standalone rewrite _retrieve_and_group
    # should search with instead of _retrieval_query's heuristic rewrite.
    # Both stay None on every off/shadow turn and on every live turn where
    # steering did not apply -- active_filters already carries whatever the
    # deterministic path decided either way.
    route_live_scope: CompiledScope | None = None
    route_live_query: str | None = None
    # Audit-only bookkeeping for why live steering did or did not apply.
    # Stays "not_attempted" whenever resolve_product() resolves (or
    # ambiguously matches) a product directly: PR12 does not touch that path
    # (existing product resolution still produces an exact product scope), so
    # the no-product branch is the only place this changes.
    route_live_outcome: str = "not_attempted"
    route_live_compiled_kind: str | None = None


_ROUTE_CLARIFY_REASONS = frozenset(
    {
        "ambiguous_product",
        "brand_lookup",
        "did_you_mean",
        "mixed_products",
        "multi_form",
        "no_product",
        "scope_warning",
        "vague_input",
    }
)


def _route_history(turns: list[PriorTurn]) -> tuple[RouteHistoryTurn, ...]:
    """Convert persisted history to the bounded, application-labelled prompt shape."""

    history: list[RouteHistoryTurn] = []
    for turn in turns:
        scope_kind = turn.scope_kind if turn.scope_audited else "none"
        policy: CorpusPolicyHint | None = None
        if scope_kind == "corpus" and turn.corpus_policy:
            try:
                policy = CorpusPolicyHint(turn.corpus_policy)
            except ValueError:
                # Bad historical metadata cannot make the shadow call fail or
                # manufacture inheritance. Downgrade it to unscoped context.
                scope_kind = "none"
        if scope_kind not in {"product", "corpus"}:
            scope_kind = "none"
        history.append(
            RouteHistoryTurn(
                question=turn.question,
                answer=turn.answer,
                scope_kind=scope_kind,
                scope_audited=turn.scope_audited and scope_kind != "none",
                corpus_policy=policy,
            )
        )
    return tuple(history)


def _current_mode_for_shadow(*, reason: str, response_mode: str) -> str:
    """Describe today's pre-model action using the route contract vocabulary."""

    if response_mode == "meta":
        return "converse"
    if response_mode in {"clarify", "scope_warning"} or reason in _ROUTE_CLARIFY_REASONS:
        return "lookup_clarify"
    return "lookup"


def _current_scope_for_shadow(state: TurnState, *, response_mode: str) -> str:
    """Describe the authoritative scope that today's path actually reached."""

    if response_mode == "meta":
        return "converse"
    if state.active_filters.get("normalized_name"):
        return "product"
    return "clarify"


def _attach_route_shadow(state: TurnState, route_json: dict[str, Any]) -> None:
    if state.route_shadow_audit is not None:
        route_json["route_call"] = dict(state.route_shadow_audit)


def _compile_route_live_scope(
    state: TurnState,
    *,
    question: str,
    session_id: str,
    session_filters: dict[str, Any],
) -> CompiledScope | None:
    """PR12: compile the live route decision into an executable scope.

    Only called from ``_resolve_and_carry_over``'s no-product branch, so
    ``resolved_product_filters`` is always None here -- a question that
    directly names a product is still resolved deterministically by
    ``resolve_product`` before this ever runs, unchanged by PR12 (plan doc
    PR12 section: "existing product resolution produces an exact product
    scope").

    Fails open like the rest of the route call: any exception here is a LIVE
    MODE failure, never a turn failure. Caught, logged, and recorded on
    ``state.route_live_outcome`` so the caller falls back to exactly the
    heuristic path shadow/off already run.
    """
    observation = state.route_shadow
    if observation is None or observation.decision is None:
        state.route_live_outcome = "route_call_failed"
        return None
    decision = observation.decision
    try:
        prior = load_prior_corpus_scope(session_id)
        prior_scope = compiled_scope_from_audit(prior.compiled_scope) if prior else None
        # The catalog read is I/O; skip it unless the decision could actually
        # need it, mirroring finalize_route_observation's same guard.
        needs_catalog = decision.scope_hint is ScopeHint.CORPUS or (
            decision.scope_hint is ScopeHint.INHERIT
            and prior_scope is not None
            and prior_scope.corpus_policy is not None
        )
        corpus_policies = load_corpus_policy_snapshots() if needs_catalog else {}
        compiled = compile_scope(
            decision,
            original_question=question,
            resolved_product_filters=None,
            session_product_filters=session_filters,
            corpus_policies=corpus_policies,
            prior_audited_scope=prior_scope,
            prior_audit_id=prior.audit_id if prior else None,
        )
    except Exception as exc:
        # Broad on purpose: a live-mode failure must degrade to the heuristic
        # path below, never surface as a turn error.
        log.warning("qa_route_live_compile_failed", error_type=type(exc).__name__)
        state.route_live_outcome = "compile_error"
        return None
    state.route_live_compiled_kind = compiled.kind.value
    return compiled


# The stage functions keep the ask_core closure names (_decline/_emit/
# _session_filters/_recent_turns) as PARAMETER names on purpose: their bodies
# are transplanted verbatim from the pre-split ask_core, and identical names
# keep that move mechanically checkable against the pre-split code.


def _resolve_and_carry_over(
    state: TurnState,
    *,
    question: str,
    session_id: str,
    s: Settings,
    _decline: Callable[..., tuple[RagOutcome, AuditPayload, SessionPatch]],
    _session_filters: Callable[[], dict[str, Any]],
) -> tuple[RagOutcome, AuditPayload, SessionPatch] | TurnState:
    """Entity resolution + session carry-over (product first, then form/route).

    Mutates ``state`` in place and returns it to continue the turn; a tuple
    return is a terminal clarify/refuse from the resolution family.
    """
    # Entity resolution FIRST: pin the product before semantic retrieval so FDA
    # template boilerplate shared across drugs cannot leak a wrong-drug citation.
    # Skip only when the caller already pinned the product (API / dossier).
    if not state.active_filters.get("normalized_name"):
        resolution = resolve_product(question)
        if resolution.status == "resolved":
            state.active_filters["normalized_name"] = resolution.normalized_name
            state.resolved_by_name = resolution.by_name
        elif resolution.status == "ambiguous":
            # Several products match, so ASK which; don't guess (cross-drug guard).
            return _decline(
                _clarify,
                reason="ambiguous_product",
                response_mode="clarify",
                interpretation="More than one product matches that. Which did you mean?",
                options=_options_from_names(resolution.candidates),
            )
        else:
            # CROSS-PRODUCT GUARD. Both candidate lookups run BEFORE the
            # carry-over, not inside its else-branch. resolve_product already
            # returned "none", so no IN-corpus product is named; a fuzzy or
            # brand hit is then the remaining evidence that the user changed
            # subject, and inheriting the session's drug there would answer a
            # question about product A using product B's guidance.
            #
            # This also closes the same hole on the pre-existing prefixes: today
            # "what about propranlol?" inherits the session product outright.
            # After this it offers the did-you-mean instead.
            #
            # Residual, unchanged and bounded: a drug ABSENT from the corpus
            # (romidepsin scores ~60, under the 82 threshold) yields neither
            # candidate, so a follow-up naming it still carries over. Closing
            # that needs a drug-name detector the resolver does not have.
            suggestions = suggest_products(question)
            # One local Drugs@FDA lookup, two answers: the corpus generics behind a
            # brand name (the long-standing did-you-mean), and whether the name
            # is a real drug this corpus simply does not carry.
            external = lookup_external_drug(question)
            brand_matches = external.corpus_products
            session_filters = _session_filters()
            # PR12: ask the live route call whether this IS the genuine
            # elliptical follow-up the word-list heuristic below keeps missing
            # (issue #219 -- "what kind of fasting? you mentioned 'similar
            # fasting'" carries neither a listed prefix nor a pronoun). Only in
            # live mode, and only once suggestions/brand_matches are known
            # empty, so a route decision can never override the did-you-mean
            # or brand-lookup guards that catch a question naming a DIFFERENT
            # product -- the same anti-leak guard the heuristic branch below
            # already relies on.
            route_scope: CompiledScope | None = None
            if s.route_call_mode == "live":
                route_scope = _compile_route_live_scope(
                    state,
                    question=question,
                    session_id=session_id,
                    session_filters=session_filters,
                )
                if route_scope is not None:
                    if route_scope.kind is not CompiledScopeKind.PRODUCT:
                        # CORPUS/CLARIFY/CONVERSE: PR12 owns compiling and
                        # authorizing these (retrieve/scope.py, unchanged),
                        # not executing them. CORPUS in particular has no
                        # $in-membership translation in the retriever yet, and
                        # the post-retrieval mixed-products guard is not
                        # scope-aware yet (that is PR13's job per the plan) --
                        # wiring execution here would either bypass that
                        # guard's intent or break every corpus turn on it. So
                        # a compiled corpus scope stays audited (see this
                        # turn's route_call.compiled_scope, unchanged from
                        # PR11b) and dark, and the turn falls through to
                        # today's deterministic path below exactly as shadow
                        # does.
                        state.route_live_outcome = f"compiled_{route_scope.kind.value}"
                        route_scope = None
                    elif suggestions or brand_matches:
                        state.route_live_outcome = "leak_guard"
                        route_scope = None
                    else:
                        state.route_live_outcome = "applied"
            route_carries_product = route_scope is not None
            if (
                session_filters.get("normalized_name")
                and not suggestions
                and not brand_matches
                and (_looks_like_follow_up(question) or route_carries_product)
            ):
                # Carry the product across turns (the chosen dosage_form/route are
                # carried just below, after resolved_name is set, so the same logic
                # also covers the single-product-corpus fallback path).
                state.active_filters["normalized_name"] = canonical_name(
                    str(session_filters["normalized_name"])
                )
                state.context_applied = True
                state.resolved_by_name = False
                if (
                    route_carries_product
                    and state.route_shadow is not None
                    and state.route_shadow.decision is not None
                ):
                    state.route_live_scope = route_scope
                    state.route_live_query = state.route_shadow.decision.standalone_question
            else:
                # No product named. Offer a high-confidence "did you mean" for genuine
                # typos, then a brand -> generic lookup (Adderall -> amphetamine);
                # else refuse (e.g. romidepsin, absent, a deliberate must-refuse).
                if suggestions:
                    return _decline(
                        _clarify,
                        reason="did_you_mean",
                        response_mode="clarify",
                        interpretation="I couldn't find that exact drug. Did you mean:",
                        options=_options_from_names(suggestions),
                        related=_options_from_names(suggestions),
                    )
                if brand_matches:
                    return _decline(
                        _clarify,
                        reason="brand_lookup",
                        response_mode="clarify",
                        interpretation=(
                            "That looks like a brand name. Did you mean its generic ingredient?"
                        ),
                        options=_options_from_names(brand_matches),
                        related=_options_from_names(brand_matches),
                    )
                if not resolution.candidates:
                    # The CORPUS is empty, not the question. Asking "which
                    # product?" would be a lie -- no answer would name one that
                    # works. A real evidence gap, so it keeps the refused
                    # register (INV-2). Sits BELOW the session carry-over and
                    # the did-you-mean/brand offers, which all have something
                    # concrete to say and must keep saying it.
                    return _decline(
                        _refuse,
                        reason="empty_corpus",
                        response_mode="refused",
                        passages=[],
                    )
                # Nothing named a product. This used to be ONE outcome
                # (reason="no_product", status="refused"), which made a
                # topic-less request and a real drug we do not carry
                # indistinguishable -- both rendered as an "Evidence gap". They
                # are separate outcomes now, and neither is a refusal.
                outcome = classify_unresolved(question, external_drug_known=external.known_absent)
                if outcome == "product_not_covered":
                    return _decline(
                        _clarify,
                        reason="product_not_covered",
                        response_mode="clarify",
                        interpretation=(
                            "I don't currently have FDA product-specific guidance for "
                            "that product in this corpus."
                        ),
                        # Empty here by construction: known_absent means Drugs@FDA
                        # matched nothing in the corpus. The catalog options the
                        # copy promises are a follow-up, not a fabrication.
                        options=_options_from_names(suggestions),
                    )
                return _decline(
                    _clarify,
                    reason="need_product",
                    response_mode="clarify",
                    interpretation=NEED_PRODUCT_GUIDANCE_TEXT,
                    options=_options_from_names(suggestions + brand_matches),
                )

    resolved_name = state.active_filters.get("normalized_name")

    # Product-scope session carry-over: a follow-up that didn't itself pin a form
    # inherits the application identity plus dosage_form/route the user already
    # chose for THIS product. Done here, after resolution, so it also covers the
    # single-product-corpus fallback, where the resolver re-pins the product and
    # the `none`-branch carry-over above never runs. Dropping appl_no here would
    # widen a same-ingredient follow-up even if persistence retained the key;
    # dropping the form pair would re-trigger the multi-form clarify.
    if (
        resolved_name
        and not state.active_filters.get("dosage_form")
        and not state.active_filters.get("route")
        # PR12: a live-steered carry-over already trusts session_filters for
        # normalized_name above, so it must trust the same session's
        # form/route too -- otherwise a multi-form product the heuristic
        # would have missed carries a product but no form, and immediately
        # hits the multi-form clarify guard below instead of retrieving.
        and (_looks_like_follow_up(question) or state.route_live_scope is not None)
    ):
        session_filters = _session_filters()
        if session_filters.get("normalized_name") == resolved_name:
            for key in ("appl_no", "dosage_form", "route"):
                if session_filters.get(key):
                    state.active_filters[key] = session_filters[key]
                    state.context_applied = True

    return state


def _pre_retrieval_route(
    state: TurnState,
    *,
    question: str,
    session_id: str,
    s: Settings,
    _decline: Callable[..., tuple[RagOutcome, AuditPayload, SessionPatch]],
    _session_filters: Callable[[], dict[str, Any]],
    _recent_turns: Callable[[], list[PriorTurn]],
) -> tuple[RagOutcome, AuditPayload, SessionPatch] | TurnState:
    """Every gate that can end the turn before retrieval runs.

    Order is load-bearing and unchanged: scope warning, meta (with its
    named-drug hard veto), resolution + carry-over, the vague-input clarify,
    then the pre-retrieval multi-form guard.
    """
    if _is_scope_warning_request(question):
        return _decline(
            _scope_warning,
            reason="scope_warning",
            response_mode="scope_warning",
            # A caller-pinned product (API/dossier filter, already
            # canonicalized by ask_core) short-circuits resolution.
            filters=state.active_filters,
        )

    # Meta gate: "what does this system do" is answered from trusted system
    # state, then one bounded guidance turn, and no retrieval. This sits AFTER
    # the scope-warning check and BEFORE entity resolution/retrieval ON PURPOSE.
    # It is a HARD VETO: fire meta only when the phrase matches AND the question
    # does NOT resolve to a named in-corpus drug. The ordering is load-bearing.
    # A named-drug question that happens to carry a meta phrase ("what BE study
    # do you cover for atorvastatin?") MUST skip meta and continue to the normal
    # grounded path, never the uncited meta answer.
    # A caller-pinned product (API/dossier filter) is likewise a resolved context,
    # so it also skips meta.
    if (
        _is_meta_request(question)
        and not state.active_filters.get("normalized_name")
        and resolve_product(question).status != "resolved"
    ):
        return _decline(_meta, reason="meta", response_mode="meta")

    # Social gate: a pleasantry is a conversation, not a failed product lookup.
    # Sits here, BEFORE entity resolution, so a greeting costs no resolver work,
    # no external product lookup and no model call -- and so it can never reach the
    # no-product branch that used to serve it an "Evidence gap" card.
    #
    # Guarded on there being no pinned or session product ON PURPOSE: "Hello"
    # with an active-ingredient filter already has a product to talk about and
    # must keep reaching the vague-input clarify below, which offers that
    # product's options. Only a greeting with nothing to anchor on converses.
    if not state.active_filters.get("normalized_name") and is_social(question):
        return _decline(
            _converse,
            reason="greeting",
            response_mode="meta",
            guide=False,
        )

    resolved = _resolve_and_carry_over(
        state,
        question=question,
        session_id=session_id,
        s=s,
        _decline=_decline,
        _session_filters=_session_filters,
    )
    if not isinstance(resolved, TurnState):
        return resolved

    # The same read _resolve_and_carry_over ended on: normalized_name is
    # settled for the remainder of the turn once resolution has run.
    resolved_name = state.active_filters.get("normalized_name")

    # Bare drug name, or no real question: guide with options instead of dumping
    # a default BE answer. Fires however the product was pinned, whether named in
    # the question, set by an API/UI filter, or carried over, so a no-topic input
    # ("Hello" with an Active-ingredient filter) never reaches the synthesizer
    # and comes back as a cited greeting. This deterministic guard owns the
    # options and status before the bounded guidance planner sees the turn.
    # A recognized drill-down ("tell me more", "why?") is EXEMPT once there is a
    # prior turn to anchor on: it is topic-less by construction, so the vague
    # gate would serve a clarify menu to a user who just asked to hear more
    # about the thing they were already discussing. _retrieval_query re-anchors
    # the embedding on that prior turn, which is what makes the exemption safe.
    #
    # The `_recent_turns()` conjunct is load-bearing, not belt-and-braces: with
    # NO history there is nothing to re-anchor on, the rewrite is the identity,
    # and exempting would trade today's useful clarify menu for a low_top_score
    # refusal. A bare drug name still clarifies -- it matches no follow-up form.
    if (
        resolved_name
        and _looks_vague(question, resolved_name)
        and not (_looks_like_follow_up(question) and _recent_turns())
    ):
        return _decline(
            _clarify,
            reason="vague_input",
            response_mode="clarify",
            interpretation=_interpretation_for(resolved_name),
            options=build_options(resolved_name),
        )

    # Multi-form guard (pre-retrieval): the resolver pins only normalized_name, but
    # ~1 in 5 drugs span multiple dosage forms/routes (e.g. estradiol: transdermal
    # gel/spray vs. vaginal tablet/insert). Blending those into one LLM context lets
    # a wrong-form PSG be cited as if it answered the question, and the blend is
    # invisible because citation labels are appl-number-only. So once a product is
    # resolved (by name OR by an API/UI filter), enumerate its CURRENT documents'
    # distinct (dosage_form, route) combos, honoring any form/route already pinned;
    # if more than one remains, CLARIFY which form before retrieving. One audit row.
    if resolved_name:
        try:
            combos = current_dosage_form_routes(
                resolved_name,
                dosage_form=state.active_filters.get("dosage_form"),
                route=state.active_filters.get("route"),
            )
        except Exception as exc:
            # This enumeration is a CORRECTNESS guard (it prevents the wrong-form
            # citation blend), so unlike the best-effort session/memory reads it
            # must NOT degrade to "no combos" -- and letting the DB error escape
            # would be an unaudited 500 the stream-fallback client re-runs into
            # the same down DB. Mirror the provider-error path instead: audited,
            # fixed-copy status="error" refusal.
            log.warning("qa_form_catalog_error", error=str(exc), error_type=type(exc).__name__)
            capture_exception(exc)
            return _decline(
                _refuse,
                reason="catalog_error",
                response_mode="refused",
                passages=[],
                status="error",
                answer_text=_SERVICE_UNAVAILABLE_TEXT,
                guide=False,
            )
        if len(combos) > 1:
            # Before clarifying, honor a form the QUESTION already names: if exactly
            # one combo's dosage_form/route tokens uniquely match the question text,
            # pin it and proceed. A form-explicit question shouldn't pay a clarify
            # hop (and on the full catalog it would otherwise flip answerable items
            # to clarify). Only a form-silent or ambiguous question clarifies.
            pinned = _combo_from_question(question, combos)
            if pinned is not None:
                state.active_filters["dosage_form"], state.active_filters["route"] = pinned
            else:
                return _decline(
                    _clarify,
                    reason="multi_form",
                    response_mode="clarify",
                    interpretation=(
                        f"{resolved_name.title()} has FDA guidance for more than one "
                        "dosage form. Which form did you mean?"
                    ),
                    options=build_form_options(resolved_name, combos, question),
                )

    return state


def _trim_evidence(passages: list[RetrievedPassage], s: Settings) -> list[RetrievedPassage]:
    """Stage 2's trim to the final evidence set.

    Args:
        passages: The wide net, already through the optional reranker.
        s: Settings; carries the final k and the diversity flag.

    Returns:
        The passages synthesis may cite. With the diversity flag off this is
        the plain first-``effective_rerank_top_k`` slice, unchanged.
    """
    if s.mmr_diversity_enabled:
        # MMR may only spend slots on passages that can survive the INV-2
        # threshold applied just below; a "diverse" sub-threshold pick would
        # be dropped there, shrinking the evidence set below k and turning
        # the diversity A/B into a count confound. When nothing clears the
        # threshold, fall through to the plain slice so the refusal path
        # (and its related-passages surface) is identical to flag-off.
        eligible = [p for p in passages if p.score >= s.refusal_score_threshold]
        if eligible:
            return mmr_select(eligible, s.effective_rerank_top_k)
    return passages[: s.effective_rerank_top_k]


def _retrieve_and_group(
    state: TurnState,
    *,
    question: str,
    k: int | None,
    s: Settings,
    _decline: Callable[..., tuple[RagOutcome, AuditPayload, SessionPatch]],
    _emit: Callable[[str], None],
    _recent_turns: Callable[[], list[PriorTurn]],
) -> tuple[RagOutcome, AuditPayload, SessionPatch] | TurnState:
    """Retrieve + rerank + threshold + the post-retrieval tripwires.

    A turn that passes every guard leaves ``state.passages`` (audit trail),
    ``state.evidence_passages`` (the only passages synthesis may see) and
    ``state.route_json`` (the retrieval route) for ``_synthesize_and_admit``.
    """
    # The retrieval boundary runs on a typed scope rather than the raw dict. It
    # is a lossless view of state.active_filters (empty values dropped, exactly
    # as _build_where would), so the WHERE clause and the mode decision are
    # unchanged -- see tests/test_product_scope.py. The dict itself still owns
    # the persistence boundaries below; only what retrieval reads moves here.
    scope = ProductScope.from_filters(state.active_filters)
    resolved_name = scope.normalized_name

    # Stage 1: wide-net vector search (up to VECTOR_TOP_K), constrained to the product.
    route_json = _route_json(
        filters=state.active_filters,
        reason="retrieval",
        context_applied=state.context_applied,
        response_mode=state.response_mode,
    )
    _attach_route_shadow(state, route_json)
    _emit("Searching the FDA guidance corpus…")
    _maybe_inject_fault("retrieve")
    # The MODE is this layer's decision, not a side effect of whether a filter
    # happens to exist downstream. Deciding it here is also what makes it
    # auditable: the mode determines the SQL and the session settings outright
    # (store.embedding_profiles.build_search_sql), so recording it records what
    # ran rather than what we hope ran.
    retrieval_scope = RetrievalScope.from_filters(scope.as_filters())
    retrieval_mode = default_mode_for_scope(retrieval_scope)
    # Embed the RE-ANCHORED query, not necessarily the user's words: a
    # contentless drill-down has no topical signal of its own. Identity for
    # every question that carries its own topic, so single-turn behaviour and
    # the offline eval are byte-identical. Orthogonal to the mode above: this
    # decides WHAT text is searched, the mode decides HOW the search runs.
    if state.route_live_query is not None:
        # PR12: the route call's standalone rewrite steered this turn instead
        # of the heuristic (_resolve_and_carry_over set both together, only
        # in live mode). It already retains every named study, metric,
        # subpart and qualifier by contract (route.py's ROUTE_SYSTEM), and it
        # is what let this turn reach retrieval at all.
        search_query = state.route_live_query
        route_json["retrieval_query_source"] = "route_live"
    else:
        search_query = _retrieval_query(
            question, normalized_name=resolved_name, prior_turns=_recent_turns()
        )
    # Persist WHETHER the rewrite fired, never the rewritten text: the audit row
    # must show that this turn searched on something other than the user's
    # words, and M7 (follow-up miss rate) counts exactly this flag.
    if search_query != question:
        route_json["retrieval_query_rewritten"] = True
    state.retrieval_block.update(
        RetrievalPlan(
            mode=retrieval_mode,
            scope=retrieval_scope,
            profile_id=(s.active_embedding_profile or "legacy").strip(),
            dimension=0,
            k=k if k is not None else s.vector_top_k,
        ).as_route_json()
    )
    passages = retrieve(search_query, k=k, filters=scope.as_filters(), mode=retrieval_mode)
    state.retrieval_block["returned"] = len(passages)
    route_json["retrieval"] = dict(state.retrieval_block)
    # Stage 2: optional rerank, then trim to RERANK_TOP_K. Same rewritten query
    # -- the reranker scores relevance against the search intent, not the
    # literal keystrokes.
    passages = rerank_passages(search_query, passages)
    # Near-clones of one section are one piece of evidence dressed as k (DSA S33).
    passages = _trim_evidence(passages, s)

    # INV-2: weak passages never enter grounded synthesis. The constrained
    # guidance model still sees the QUESTION and trusted route/options, but not
    # the sub-threshold passage text, so it can choose a useful next step without
    # letting irrelevant evidence become citation cover. Gate on the
    # MAX cosine score, not passages[0]: the reranker (when enabled) reorders by
    # a cross-encoder score on a different scale, so passages[0].score may be a
    # demoted-but-still-present passage's cosine value. The 0.30 threshold sits
    # on the cosine scale, so compare the true best cosine.
    if not passages or max(p.score for p in passages) < s.refusal_score_threshold:
        return _decline(
            _refuse,
            reason="low_top_score",
            response_mode="refused",
            passages=passages,
            # Surface the sub-threshold matches as inert "related" pointers
            # (distinct product NAMES + source link only). refused/citations
            # stay untouched, so this never dresses the refusal as an answer.
            related=_related_from_passages(passages),
        )

    # One strong hit must not launder weaker neighbors into the prompt or
    # citation allowlist. Keep all retrieved rows in the audit trail, but only
    # individually above-threshold passages may support synthesis.
    evidence_passages = [
        passage for passage in passages if passage.score >= s.refusal_score_threshold
    ]

    # Post-retrieval guard (defense in depth): every passage must be the same
    # product. The filter guarantees this; this catches a caller that bypassed
    # the resolver. Mixed products means CLARIFY which (offer the distinct
    # products) rather than cite across them or bluntly refuse: the evidence is
    # unclear, so ask. Zero citations either way (never fabricates).
    distinct_products = sorted({p.normalized_name for p in passages if p.normalized_name})
    if len(distinct_products) > 1:
        return _decline(
            _clarify,
            reason="mixed_products",
            response_mode="clarify",
            interpretation="These passages span more than one product. Which did you mean?",
            options=_options_from_names(distinct_products),
            # Post-retrieval tripwire: audit WHAT was retrieved (the
            # cross-product evidence is the whole point of the row).
            passages=passages,
        )

    # Same defense in depth for dosage form: even within one product, passages must
    # not blend distinct (dosage_form, route) combos (the wrong-form-citation bug).
    # The pre-retrieval guard normally catches this; this backstops a caller that
    # bypassed it. Offer one option per combo, pinning form+route. Skipped when a
    # passage is missing form/route metadata (a half-known combo would split docs
    # that are answerable together, e.g. same-combo beclomethasone docs).
    passage_combos = {
        (str(p.metadata.get("dosage_form")), str(p.metadata.get("route")))
        for p in passages
        if p.metadata.get("dosage_form") and p.metadata.get("route")
    }
    one_product = distinct_products[0] if distinct_products else resolved_name
    if one_product and len(passage_combos) > 1:
        return _decline(
            _clarify,
            reason="multi_form",
            response_mode="clarify",
            interpretation=(
                "These passages span more than one dosage form. Which form did you mean?"
            ),
            options=build_form_options(one_product, sorted(passage_combos), question),
            # Post-retrieval tripwire: audit WHAT was retrieved (the
            # cross-form evidence is the whole point of the row).
            passages=passages,
        )

    state.passages = passages
    state.evidence_passages = evidence_passages
    state.route_json = route_json
    return state


def _synthesize_and_admit(
    state: TurnState,
    *,
    question: str,
    session_id: str,
    turn_id: str,
    user_id: str | None,
    s: Settings,
    _decline: Callable[..., tuple[RagOutcome, AuditPayload, SessionPatch]],
    _emit: Callable[[str], None],
    _recent_turns: Callable[[], list[PriorTurn]],
    _emit_draft: Callable[[str], None] | None = None,
    _emit_draft_reset: Callable[[], None] | None = None,
) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
    """Prompt build + the one synthesis call + the admit gate + verdicts.

    Always terminal: every branch returns the (outcome, audit, patch) triple.
    """
    passages = state.passages
    evidence_passages = state.evidence_passages
    route_json = state.route_json
    resolved_name = state.active_filters.get("normalized_name")

    _emit(f"Reading {len(evidence_passages)} matching guidance passage(s)…")
    # Conversational memory: thread the last few ANSWERED turns so a follow-up
    # ("what about the fed study?") resolves naturally. Context ONLY: citations
    # are stripped (_format_recent) and the system prompt forbids treating it as a
    # source; INV-1 still holds because _validate_citations accepts only THIS
    # turn's passages, so a fact that lived only in a prior turn cannot acquire a
    # valid citation here and is dropped/refused. The current turn's just-written
    # user row is excluded by turn_id (the shell bakes that into the loader).
    # With no usable history the block is "" so the prompt is byte-identical to
    # the single-turn form (protects the eval).
    recent_block = _format_recent(_recent_turns())
    recent_context = (
        "Recent conversation (context ONLY — use it to resolve pronouns and "
        "ellipsis in the question; it is NOT a source and MUST NOT be cited or "
        "treated as fact):\n<untrusted_recent_conversation>\n"
        f"{recent_block}\n</untrusted_recent_conversation>\n\n"
        if recent_block
        else ""
    )
    # v6 prose vs v5 claims-JSON is a per-turn read, not a boot decision: the
    # flag is a Fly secret flip with no deploy, and the audit row must stamp
    # the identity of the path that actually produced it.
    prose_mode = bool(getattr(s, "prose_synthesis_enabled", False))
    # v7 selective citation is a POLICY change on top of v6's FORMAT change,
    # and only makes sense as a prose prompt -- honored only when prose is
    # also on. A misconfigured selective-without-prose is inert (this reads
    # False and the turn runs the v5 path exactly) but observable (risk 6).
    selective_mode = prose_mode and bool(getattr(s, "selective_citation_enabled", False))
    if bool(getattr(s, "selective_citation_enabled", False)) and not prose_mode:
        log.warning("selective_citation_without_prose")
    active_prompt = (
        GROUNDED_QA_PROMPT_V7
        if selective_mode
        else (GROUNDED_QA_PROMPT_V6 if prose_mode else GROUNDED_QA_PROMPT)
    )
    if selective_mode:
        user_prompt = GROUNDED_QA_USER_V7.format(
            recent_context=recent_context,
            question=question,
            passages=_format_passages_numbered(evidence_passages),
        )
    elif prose_mode:
        user_prompt = GROUNDED_QA_USER_V6.format(
            recent_context=recent_context,
            question=question,
            passages=_format_passages_numbered(evidence_passages),
        )
    else:
        user_prompt = GROUNDED_QA_USER.format(
            recent_context=recent_context,
            question=question,
            passages=_format_passages(evidence_passages),
        )
    route_json["prompt"] = active_prompt.as_dict()

    _emit("Composing a cited answer…")
    log.info("llm_prompt", role="synthesizer", **active_prompt.log_fields())
    provider = get_llm_provider(role="synthesizer")
    if selective_mode:
        # v7 sends NO schema message either, like v6 -- see the v6 comment
        # below for why. Own system prompt and own exemplar set (B.10.2), same
        # user/assistant message shape.
        synth_messages = [LLMMessage(role="system", content=GROUNDED_QA_SYSTEM_V7)]
        synth_messages.extend(
            LLMMessage(role=exemplar_role, content=exemplar_text)
            for exemplar_role, exemplar_text in GROUNDED_QA_EXEMPLARS_V7
        )
        synth_messages.append(LLMMessage(role="user", content=user_prompt))
    elif prose_mode:
        # v6 sends NO schema message anywhere -- the schema is the server-side
        # parser now -- and the few-shot exemplars ride between the system
        # prompt and the live turn as real user/assistant pairs. The tail
        # restatement inside GROUNDED_QA_USER_V6 is the true last-read text on
        # the Databricks path (all system content is front-loaded on that wire).
        synth_messages = [LLMMessage(role="system", content=GROUNDED_QA_SYSTEM_V6)]
        synth_messages.extend(
            LLMMessage(role=exemplar_role, content=exemplar_text)
            for exemplar_role, exemplar_text in GROUNDED_QA_EXEMPLARS_V6
        )
        synth_messages.append(LLMMessage(role="user", content=user_prompt))
    else:
        synth_messages = [
            LLMMessage(role="system", content=GROUNDED_QA_SYSTEM),
            LLMMessage(role="user", content=user_prompt),
            # The schema rides as a TRAILING system message, so it is the last
            # thing the model reads and it never has to survive a .format() pass.
            TURN_SCHEMA_MESSAGE,
        ]
    # What the synthesis call actually cost and whether it had to retry. Two
    # facts that lived only in a structlog line (or, for the retry, nowhere
    # durable at all), which is why "is our malformed_structure rate a token
    # cap hit or a JSON syntax error?" is unanswerable from the DB today.
    # synth_route holds the SAME dict object, not a copy: _complete_structured
    # fills it in during the call, and every branch below reads it afterwards.
    synth_telemetry: dict[str, Any] = {"max_output_tokens": s.synthesizer_max_tokens}
    synth_route: dict[str, Any] = {"synthesis": synth_telemetry}

    def _run_synthesis(messages: list[LLMMessage]) -> Any:
        """One synthesis completion over ``messages``.

        Extracted so the bounds repair below can issue its second attempt
        through the SAME branch logic (streamed vs buffered, prose vs JSON)
        rather than a copy that could drift from it.
        """
        if prose_mode and _emit_draft is not None:
            return _stream_structured(
                provider,
                messages,
                max_tokens=s.synthesizer_max_tokens,
                telemetry=synth_telemetry,
                on_delta=_emit_draft,
                # A missing reset sink degrades to a no-op, not a hold: the
                # caller opted into drafts but not the retroactive-discard
                # signal (e.g. a direct ask() call in tests), and
                # _stream_structured's own try/except already treats a
                # failing sink as cosmetic.
                on_reset=_emit_draft_reset or (lambda: None),
            )
        return _complete_structured(
            provider,
            messages,
            max_tokens=s.synthesizer_max_tokens,
            response_format=None if prose_mode else "json",
            telemetry=synth_telemetry,
        )

    try:
        response = _run_synthesis(synth_messages)
    except Exception as exc:  # provider transport error (timeout / 429 / 5xx)
        # B2: a synthesizer failure must NOT return a naked 500 with no audit
        # row, which would break INV-6 exactly when the system misbehaves. We
        # degrade to a graceful, audited refusal (status="error") and surface
        # the cause to Sentry. The error never reaches the user verbatim.
        log.warning("qa_provider_error", error=str(exc), error_type=type(exc).__name__)
        capture_exception(exc)
        return _decline(
            _refuse,
            reason="provider_error",
            response_mode="refused",
            passages=passages,
            status="error",
            answer_text=_SERVICE_UNAVAILABLE_TEXT,
            route_extra=synth_route,
            guide=False,
        )
    answer = response.text.strip()

    # INV-1/INV-2: a degenerate completion (empty after stripping, e.g. a
    # max_tokens truncation or a provider hiccup) is not an answer. Refuse
    # rather than fall through and emit a non-refused, zero-citation empty
    # "answer".
    if not answer:
        return _decline(
            _refuse,
            reason="empty_completion",
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            usage=response.usage,
            guide=False,
        )

    # Pathological-output bounds (issue #183). The prose arms apply no length
    # bound at admission by design, so a degenerate completion -- a repetition
    # loop, a sentence that never terminates -- would otherwise render
    # unbounded text. ONE repair attempt, then a conversational exit.
    #
    # One, not a loop: a model producing a runaway sentence is in a state a
    # second identical nudge does not fix, and each attempt costs a full
    # synthesis call on the user's latency budget.
    if prose_mode:
        breach = prose_turn.bounds_exceeded(answer)
        if breach is not None:
            log.warning("qa_prose_bounds_breach", breach=breach, chars=len(answer))
            synth_telemetry["bounds_breach"] = breach
            synth_telemetry["bounds_repair_attempted"] = True
            # Whatever the client already rendered came from the breaching
            # completion, so retract it before the second attempt streams over
            # it. Same contract the truncation retry uses.
            if _emit_draft_reset is not None:
                try:
                    _emit_draft_reset()
                except Exception:  # cosmetic sink; never fail the turn on it
                    log.debug("on_draft_reset_failed", exc_info=True)
            try:
                response = _run_synthesis([*synth_messages, *_BOUNDS_REPAIR_TURN[breach]])
            except Exception as exc:
                log.warning("qa_provider_error", error=str(exc), error_type=type(exc).__name__)
                capture_exception(exc)
                return _decline(
                    _refuse,
                    reason="provider_error",
                    response_mode="refused",
                    passages=passages,
                    status="error",
                    answer_text=_SERVICE_UNAVAILABLE_TEXT,
                    route_extra=synth_route,
                    guide=False,
                )
            answer = response.text.strip()
            still = prose_turn.bounds_exceeded(answer) if answer else breach
            synth_telemetry["bounds_repair_succeeded"] = still is None
            if still is not None:
                # The reason is machine-readable and stays out of the reply:
                # nothing about the QUESTION was wrong, only our answer to it.
                log.warning("qa_prose_bounds_repair_failed", breach=still)
                return _decline(
                    _refuse,
                    reason=_BOUNDS_REASON[still],
                    response_mode="refused",
                    passages=passages,
                    model_name=response.model,
                    answer_text=tg.OVERSIZE_RECOVERY_TEXT,
                    usage=response.usage,
                    route_extra=synth_route,
                    guide=False,
                )

    _emit("Checking each claim against its source…")
    admitted: AdmittedTurn | GateFailure
    parsed_prose: prose_turn.ParsedProseTurn | None = None
    if prose_mode:
        parsed_prose = prose_turn.parse(
            answer, passages=evidence_passages, selective=selective_mode
        )
        # Nested under the existing "synthesis" telemetry block on purpose: the
        # route_json TOP-level key set is contract-pinned, and the parse record
        # is synthesis forensics (INV-6) -- what the parser killed and why.
        synth_telemetry["prose_parse"] = {
            "claims_parsed": len(parsed_prose.claims),
            "killed": [t[:400] for t in parsed_prose.leftover_brackets],
            "truncated_material": parsed_prose.truncated_material,
        }
        if selective_mode:
            # CONDITIONAL: emitted only under v7, so v6's ledger bytes (and
            # tests/test_prose_synthesis.py:102-106's exact-dict pin) never move.
            synth_telemetry["prose_parse"]["kinds"] = [c.kind for c in parsed_prose.claims]
        killed_material: str | None = None
        for killed in parsed_prose.leftover_brackets:
            killed_material = tg.materiality_trigger(killed)
            if killed_material is not None:
                break
        if parsed_prose.truncated_material or killed_material is not None:
            # The parser removed materially-worded text (an unterminated tail
            # or a bracket-killed sentence). What survives could read as its
            # own opposite, so the whole answer is rejected -- the same OD-4
            # stance as the gate's material-drop verdict, one layer earlier.
            log.warning(
                "qa_prose_parse_material_kill",
                truncated=parsed_prose.truncated_material,
                material_word=killed_material,
            )
            return _decline(
                _refuse,
                reason="material_drop",
                response_mode="refused",
                passages=passages,
                model_name=response.model,
                answer_text=tg.MATERIAL_DROP_TEXT,
                usage=response.usage,
                route_extra=synth_route,
                guide=False,
            )
        if parsed_prose.turn_type == "ANSWER" and not parsed_prose.claims:
            # Reason string kept as malformed_structure so the ops greps and
            # the ~12% prod baseline stay comparable; in prose mode it means
            # "no sentences parsed", not "schema violation".
            admitted = GateFailure(
                "malformed_structure", "no sentences parsed from prose completion"
            )
        elif selective_mode:
            admitted = admit_claims(
                parsed_prose.turn_type,
                prose_turn.to_claims(parsed_prose, evidence_passages),
                passages=evidence_passages,
                question=question,
                correct=True,
                downgrade_uncited=False,
                kinds=[c.kind for c in parsed_prose.claims],
                selective=True,
            )
        else:
            admitted = admit_claims(
                parsed_prose.turn_type,
                prose_turn.to_claims(parsed_prose, evidence_passages),
                passages=evidence_passages,
                question=question,
                # v6 branch. Re-stamp correction is live (v6 is still cite or
                # refuse, and a corrected claim is a CITED claim); the
                # uncited-downgrade path is not. Serving uncited prose is v7's
                # policy, and v7 gets there through selective=True above.
                correct=True,
                downgrade_uncited=False,
            )
    else:
        admitted = admit_turn(answer, passages=evidence_passages, question=question)
    if isinstance(admitted, GateFailure):
        # A parse failure asserts something about the MACHINE, never about the
        # corpus. Serving settings.refusal_text here ("I couldn't find this in
        # the current FDA guidance corpus") would record a claim about coverage
        # that was never tested, in the audit row, forever.
        log.warning("qa_malformed_structure", detail=admitted.detail[:500])
        return _decline(
            _refuse,
            reason=admitted.reason,
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            status="error",
            answer_text=_SERVICE_UNAVAILABLE_TEXT,
            usage=response.usage,
            route_extra={
                **synth_route,
                "gate_failure": {
                    "class": _gate_failure_class(admitted.detail),
                    "detail": admitted.detail[:200],
                },
            },
            guide=False,
        )

    if (
        parsed_prose is not None
        and parsed_prose.leftover_brackets
        and admitted.verdict == tg.VERDICT_ANSWER
    ):
        # OD-5 continuity for parser kills: a benign bracket-killed sentence
        # never reached the gate, so the verdict alone would render a clean
        # fully-cited answer while text was silently removed. Folding to
        # PARTIAL makes render_answer disclose the omission in the same plain
        # language as a gate drop; the removed sentences themselves are in
        # synth_telemetry["prose_parse"]["killed"].
        admitted = replace(admitted, verdict=tg.VERDICT_PARTIAL)

    log.info("qa_turn_gate", **_gate_log_fields(admitted))

    # v7 found-nothing: the model's own decline text, re-scanned against the
    # materiality/source-assertion lexicons before it can be served (B.10.1.2).
    # Computed here (only for the one verdict render_decline is documented to
    # accept) so the ledger below can carry the guard on every branch.
    decline_text: str | None = None
    decline_guard: str | None = None
    if admitted.verdict == tg.VERDICT_CONVERSATIONAL_DECLINE:
        decline_text, decline_guard = tg.render_decline(admitted)
        if decline_guard is not None:
            # The parser and the gate disagreed (a caller passing kinds that
            # do not match the texts) -- defense in depth fired. Never serves
            # the guarded text; the caller below falls back to canned copy.
            log.warning("qa_decline_guard_fallback", guard=decline_guard)

    # OD-5's operator half rides on EVERY post-gate audit row, not just the
    # answer path. The decline branches below are exactly the turns where a
    # claim was DROPPED (no_valid_citations, material_drop), so persisting the
    # ledger only on the answer path would leave the per-claim drop reason and
    # the offending (short_name, page) pairs in a structlog line and nowhere in
    # the DB -- the opposite of what turn_gate.ledger claims to provide. Built
    # once here so the answer and decline paths cannot drift.
    turn_route = {
        **synth_route,
        "turn": tg.ledger(
            admitted,
            model=response.model,
            prompt_version=active_prompt.version,
            renderer_version=(
                tg.RENDERER_VERSION_SELECTIVE if selective_mode else tg.RENDERER_VERSION
            ),
            decline_guard=decline_guard,
        ),
    }

    if admitted.verdict == tg.VERDICT_NO_EVIDENCE:
        # The model declined. Unchanged two-way branch: the user named a real
        # drug but the model couldn't answer this phrasing (the live net for
        # vague inputs `_looks_vague` didn't catch) -> guide. When the product
        # came from the single-product fallback (no drug named), a decline is a
        # genuine "not covered" -> stay refused (INV-2).
        if state.resolved_by_name and resolved_name:
            return _decline(
                _clarify,
                reason="model_refusal",
                response_mode="clarify",
                # Post-retrieval decline: keep the passages that the model saw
                # and judged insufficient. These are the only scored negatives
                # the system produces -- a real product resolved, retrieval ran,
                # and the evidence still did not support an answer -- so without
                # them the audit row cannot say WHAT was weak, and
                # REFUSAL_SCORE_THRESHOLD (0.30, never calibrated) has no
                # observations to be calibrated against. Same rationale as the
                # mixed_products / multi_form backstops below.
                passages=passages,
                model_name=response.model,
                interpretation=_interpretation_for(resolved_name),
                options=build_options(resolved_name),
                usage=response.usage,
                route_extra=turn_route,
                guide=False,
            )
        return _decline(
            _refuse,
            reason="model_refusal",
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            usage=response.usage,
            route_extra=turn_route,
            guide=False,
        )

    if admitted.verdict == tg.VERDICT_NO_VALID_CITATIONS:
        # Every claim failed the gate (or the model emitted none). NOT a
        # no-evidence turn: see the note in turn_gate.
        return _decline(
            _refuse,
            reason="no_valid_citations",
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            usage=response.usage,
            route_extra=turn_route,
            guide=False,
        )

    if admitted.verdict == tg.VERDICT_MATERIAL_DROP:
        # OD-4: what was dropped carried obligation/permission/exception
        # wording, so the surviving claims can read as their own opposite.
        # Reject the whole answer rather than hand back a confident, fully
        # cited, faithfulness-1.0 statement with the qualifier deleted.
        return _decline(
            _refuse,
            reason="material_drop",
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            answer_text=tg.MATERIAL_DROP_TEXT,
            usage=response.usage,
            route_extra=turn_route,
            guide=False,
        )

    if admitted.verdict == tg.VERDICT_CONVERSATIONAL_DECLINE:
        # v7 found-nothing: the model says so in its own words. Wire shape is
        # the v5/v6 refusal contract exactly (refused, citations=[], reason
        # model_refusal); only the TEXT is model-authored, and only after
        # render_decline re-scanned every sentence it is about to serve. Never
        # forks to _clarify (AMENDMENT 1, B.10.1.3): that REPLACES the answer
        # text with application copy plus options, which would discard the
        # model's prose -- the entire feature.
        return _decline(
            _refuse,
            reason="model_refusal",
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            # None (a guard fired) -> _refuse serves s.refusal_text (the
            # canned copy), never the guarded text.
            answer_text=decline_text,
            usage=response.usage,
            route_extra=turn_route,
            guide=False,
        )

    rendered_answer = tg.render_answer(admitted)
    # Enrich once, here, so the SAME citations reach the audit row, the chat
    # history and the wire. Doing it on the response path only was what made
    # provenance decay on reload.
    citations = _enrich_citation_recency(tg.citations(admitted))
    route_json["partial_evidence"] = bool(admitted.unsupported)
    route_json.update(turn_route)

    audited = _audit_retrieved(passages)
    outcome = RagOutcome(
        answer=rendered_answer,
        citations=citations,
        refused=False,
        model_name=response.model,
        retrieved=audited,
        status=state.response_mode,
        reason=route_json.get("reason"),
        session_id=session_id,
        turn_id=turn_id,
        claim_tags=tg.claim_tags(admitted),
    )
    audit = AuditPayload(
        mode="qa",
        query_text=question,
        retrieved=audited,
        answer_text=rendered_answer,
        citations=[asdict(c) for c in citations],
        refused=False,
        model_name=response.model,
        session_id=session_id,
        turn_id=turn_id,
        user_id=user_id,
        status=state.response_mode,
        route_json=route_json,
        # No-audit-no-answer (INV-6): a validated answer with no audit row is
        # never returned -- but the failure must be DEFINED, not a naked 500
        # that the stream-fallback client re-runs (a second paid synthesis)
        # into the same down DB. allow_skip=False makes the shell use the
        # STRICT write; on failure it serves this fallback -- the fixed-copy
        # status="error" refusal, whose own audit is re-attempted and skipped
        # (flagged) if the DB is still down. Built eagerly (it is pure):
        # state.active_filters/state.context_applied no longer mutate after synthesis, so
        # build-time and failure-time route_json are identical.
        allow_skip=False,
        failure_fallback=_decline(
            _refuse,
            reason="audit_error",
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            status="error",
            answer_text=_SERVICE_UNAVAILABLE_TEXT,
            usage=response.usage,
            # If the strict write fails this fallback row is the ONLY record of
            # the turn, so it carries the ledger too -- otherwise a PARTIAL
            # verdict's drops would vanish exactly when the DB is misbehaving.
            route_extra=turn_route,
            guide=False,
        ),
        **_usage_fields(response.model, response.usage),
    )
    return (
        outcome,
        audit,
        _build_patch(outcome, filters=state.active_filters, route_json=route_json),
    )


def ask_core(
    question: str,
    *,
    session_id: str,
    turn_id: str,
    filters: dict[str, Any] | None = None,
    k: int | None = None,
    user_id: str | None = None,
    load_session_filters: Callable[[], dict[str, Any]],
    load_recent_turns: Callable[[], list[PriorTurn]],
    on_progress: Callable[[str], None] | None = None,
    on_draft: Callable[[str], None] | None = None,
    on_draft_reset: Callable[[], None] | None = None,
) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
    """The PURE compute half of a turn: load context -> compute -> describe.

    Performs NO persistence on ANY path (success, refusal, clarify, meta, and
    error paths alike): every branch returns (RagOutcome, AuditPayload,
    SessionPatch) and the ``ask()`` shell -- later, the Go control plane --
    performs the writes. Reads are allowed (retrieval reads the vector store,
    the resolver reads products); session context comes in through the two
    shell-owned loaders, invoked lazily at exactly the pre-split call points so
    turns that never carry context over still skip the reads.

    ``session_id``/``turn_id`` are the SHELL's ids (already ensured/degraded);
    the core only threads them into what it returns.

    ``on_progress`` behaves exactly as documented on ``ask()``: cosmetic,
    best-effort, never answer-bearing. There is deliberately NO token sink here
    -- answer text is replayed by the shell after the audit write. The ONLY
    un-gated bytes the core may emit ride the dual-gated provisional draft
    channel (``on_draft``/``on_draft_reset``, owner-amended INV-1, 2026-08-10);
    they are never presented as validated.
    """
    s = get_settings()

    def _emit(textline: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(textline)
        except Exception:  # broad: progress is best-effort, never fatal
            log.debug("on_progress_failed", exc_info=True)

    model_name = current_model_name(role="synthesizer")
    active_filters: dict[str, Any] = dict(filters or {})
    # Product-key hardening: a caller (API / dossier / clarify option) may pass a
    # normalized_name in any casing or salt-order. Canonicalize it to the exact key
    # the corpus stores (canonical_name) so retrieval's exact-match filter cannot
    # silently miss and turn a real product into a wrong refusal.
    if active_filters.get("normalized_name"):
        active_filters["normalized_name"] = canonical_name(str(active_filters["normalized_name"]))

    # Up to two carry-over sites below read the session filters (product in the
    # resolver's none-branch, then dosage_form/route after resolution). Nothing
    # mutates them mid-turn (the shell applies filter updates after the turn),
    # so fetch lazily ONCE and reuse instead of two identical row reads per
    # follow-up. Lazy so turns that never carry over still skip the read.
    session_filters_memo: dict[str, Any] | None = None

    def _session_filters() -> dict[str, Any]:
        nonlocal session_filters_memo
        if session_filters_memo is None:
            session_filters_memo = load_session_filters()
        return session_filters_memo

    recent_turns_memo: list[PriorTurn] | None = None

    def _recent_turns() -> list[PriorTurn]:
        """History, loaded at most once per turn.

        Two callers now need it -- the retrieval rewrite (before the vector
        search) and the synthesizer's conversation block (after it) -- and
        ``load_recent_turns`` is a DB read. Memoized so widening the follow-up
        path does not double the per-turn query count.
        """
        nonlocal recent_turns_memo
        if recent_turns_memo is None:
            recent_turns_memo = load_recent_turns()
        return recent_turns_memo

    response_mode: QueryStatusLiteral = "summary" if _is_summary_request(question) else "answer"
    state = TurnState(active_filters=active_filters, response_mode=response_mode)
    # Shadow context uses independent reads. It must never populate the
    # authoritative memo above before today's deterministic pipeline reaches
    # its normal read point: doing so would shift the snapshot used by a
    # concurrent-session follow-up and could change real behavior.
    route_shadow_session_filters: dict[str, Any] = {}

    def _finalize_route_shadow(*, reason: str, response_mode: str) -> None:
        """Compile and compare once, without changing any authoritative state."""

        if state.route_shadow is None or state.route_shadow_audit is not None:
            return
        # The INHERIT leg is unobservable without an audited prior scope: every
        # inherit hint would compile to CORPUS_INHERITANCE_UNAUDITED and the
        # window would report that inheritance never works when it was never
        # asked. Read independently of the authoritative memo, like the rest of
        # the shadow's context, so a bad read cannot perturb this turn.
        prior_corpus = load_prior_corpus_scope(session_id)
        finalized = finalize_route_observation(
            state.route_shadow,
            original_question=question,
            resolved_product_filters=(
                state.active_filters if state.active_filters.get("normalized_name") else None
            ),
            session_product_filters=route_shadow_session_filters,
            load_corpus_policies=load_corpus_policy_snapshots,
            prior_audited_scope=(
                compiled_scope_from_audit(prior_corpus.compiled_scope) if prior_corpus else None
            ),
            prior_audit_id=prior_corpus.audit_id if prior_corpus else None,
            current_mode=_current_mode_for_shadow(
                reason=reason,
                response_mode=response_mode,
            ),
            current_scope=_current_scope_for_shadow(
                state,
                response_mode=response_mode,
            ),
            current_reason=reason,
        )
        state.route_shadow_audit = finalized.audit
        if finalized.error is not None:
            log.warning(
                "qa_route_shadow_compile_error",
                error_type=type(finalized.error).__name__,
            )
        if s.route_call_mode == "live":
            # PR12: what actually steered this turn, distinct from
            # finalize_route_observation's agrees_with_mode/scope above (which
            # compare the route's PROPOSAL to today's path regardless of
            # whether live mode used it). "applied" means
            # _resolve_and_carry_over's carry-over ran off route_live_scope's
            # compiled product filters and standalone rewrite, not the
            # word-list heuristic; every other outcome describes why it did
            # not, including a live route call that itself failed.
            state.route_shadow_audit["live_steering"] = {
                "applied": state.route_live_scope is not None,
                "outcome": state.route_live_outcome,
                "compiled_kind": state.route_live_compiled_kind,
            }

    def _decline(
        maker: Callable[..., tuple[RagOutcome, AuditPayload]],
        *,
        reason: str,
        response_mode: str,
        route_extra: dict[str, Any] | None = None,
        guide: bool = True,
        **kw: Any,
    ) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
        """One ceremony for every terminal decline (_refuse/_clarify/_scope_warning/
        _meta) branch: build the audit route_json and the result TOGETHER so the
        reason/response_mode pairing is single-source -- a branch can no longer
        record an audit route that silently disagrees with the turn it describes.
        Reads state.active_filters/state.context_applied at CALL time (they mutate as the
        pipeline advances); post-synthesis branches override model_name
        (response.model) and pass usage via **kw.

        ``route_extra`` and ``guide`` are NAMED keywords, so neither is forwarded
        to ``maker`` via **kw. Healthy PRE-synthesis terminal branches keep
        ``guide=True`` and therefore attempt one constrained router completion;
        post-synthesis/error branches set it false because an AI call already
        happened or the failure makes another call inappropriate."""
        rj = _route_json(
            filters=state.active_filters,
            reason=reason,
            context_applied=state.context_applied,
            response_mode=response_mode,
        )
        # Declines that happen AFTER stage-1 search still describe a retrieval
        # that ran, so the plan belongs on their audit row too. _decline builds
        # a FRESH rj, so it cannot inherit the answer path's mutation.
        if state.retrieval_block:
            rj["retrieval"] = dict(state.retrieval_block)
        if route_extra:
            rj.update(route_extra)
        _finalize_route_shadow(reason=reason, response_mode=response_mode)
        _attach_route_shadow(state, rj)
        kw.setdefault("model_name", model_name)
        outcome, audit = maker(
            question=question,
            reason=reason,
            session_id=session_id,
            turn_id=turn_id,
            user_id=user_id,
            route_json=rj,
            **kw,
        )

        # The planner earns its round trip only when it can change something.
        # On these two reasons the rendered copy is fixed at the call site, so
        # with no options to order the call is pure cost -- which is what every
        # "Hello" used to pay. Deliberately NARROW: low_top_score, model_refusal,
        # no_valid_citations and material_drop pick between two sentences via
        # plan.next_step (guidance.render_guidance_message), so they keep calling
        # even with no options.
        if reason in {"need_product", "product_not_covered"} and not (
            outcome.clarify or outcome.related
        ):
            guide = False
        if guide and outcome.status != "error":
            # Every healthy Ask turn reaches exactly one AI path. On a branch that
            # cannot safely synthesize a cited answer, the router model selects a
            # server-allowlisted NEXT STEP and existing option IDs. It never writes
            # display prose, changes status, invents filters, or sees weak passage
            # text. A provider/shape failure keeps the trusted deterministic reply.
            product = str(state.active_filters.get("normalized_name") or "").strip() or None
            # An ambiguous/suggested candidate is not trusted product context.
            # Scope guidance is the one handler that may resolve a product inside
            # its deterministic maker without updating state.active_filters; recover it
            # only when every application-authored option agrees.
            if product is None and reason == "scope_warning":
                candidates = {
                    str(candidate)
                    for option in outcome.related
                    if (candidate := (option.filters or {}).get("normalized_name"))
                }
                if len(candidates) == 1:
                    product = candidates.pop()
            request = build_guidance_request(
                question=question,
                status=outcome.status,
                reason=reason,
                product=product,
                clarify=outcome.clarify,
                related=outcome.related,
            )
            rj["prompt"] = QUERY_GUIDANCE_PROMPT.as_dict()
            rj["guidance"] = {"attempted": True, "applied": False}
            router_model_name = current_model_name(role="router")
            outcome.model_name = router_model_name
            audit.model_name = router_model_name
            log.info("llm_prompt", role="router", **QUERY_GUIDANCE_PROMPT.log_fields())
            try:
                guide_response = _complete_structured(
                    get_llm_provider(role="router"), request.messages, max_tokens=600
                )
            except D1ResidencyError:
                # Same fail-closed residency behavior as grounded synthesis: the
                # outer audited pipeline boundary converts this into status=error.
                raise
            except Exception as exc:
                log.warning(
                    "qa_guidance_provider_error",
                    error_type=type(exc).__name__,
                )
                capture_exception(exc)
                rj["guidance"]["fallback_reason"] = "provider_error"
            else:
                outcome.model_name = guide_response.model
                audit.model_name = guide_response.model
                for field_name, value in _usage_fields(
                    guide_response.model, guide_response.usage
                ).items():
                    setattr(audit, field_name, value)
                try:
                    plan = parse_guidance_plan(guide_response.text, request)
                except ValueError as exc:
                    log.warning("qa_guidance_invalid", reason=str(exc))
                    rj["guidance"]["fallback_reason"] = str(exc)
                else:
                    message = render_guidance_message(
                        plan,
                        reason=reason,
                        product=product,
                        fallback=outcome.answer,
                    )
                    outcome.answer = message
                    audit.answer_text = message
                    if outcome.status == "clarify":
                        outcome.interpretation = message
                    outcome.clarify = prioritize_options(
                        outcome.clarify, channel="clarify", plan=plan
                    )
                    outcome.related = prioritize_options(
                        outcome.related, channel="related", plan=plan
                    )
                    rj["guidance"] = {
                        "attempted": True,
                        "applied": True,
                        "next_step": plan.next_step,
                        "option_ids": list(plan.option_ids),
                        "selected_options": selected_option_records(plan, request),
                    }
        return outcome, audit, _build_patch(outcome, filters=state.active_filters, route_json=rj)

    if s.route_call_mode != "off":
        if s.route_call_mode == "live":
            log.warning("qa_route_live_reserved_shadow")
        context_failures: list[str] = []
        try:
            route_shadow_session_filters = load_session_filters()
        except Exception as exc:
            context_failures.append("session_filters")
            log.warning(
                "qa_route_shadow_context_failed",
                context="session_filters",
                error_type=type(exc).__name__,
            )
        try:
            route_shadow_recent_turns = load_recent_turns()
        except Exception as exc:
            route_shadow_recent_turns = []
            context_failures.append("recent_turns")
            log.warning(
                "qa_route_shadow_context_failed",
                context="recent_turns",
                error_type=type(exc).__name__,
            )
        trusted_product = (
            str(state.active_filters.get("normalized_name") or "").strip()
            or str(route_shadow_session_filters.get("normalized_name") or "").strip()
            or None
        )
        log.info("llm_prompt", role="router", **ROUTE_PROMPT.log_fields())
        state.route_shadow = observe_route(
            provider_factory=lambda: get_llm_provider(role="router"),
            configured_model_name=current_model_name(role="router"),
            configured_mode=s.route_call_mode,
            question=question,
            recent_turns=_route_history(route_shadow_recent_turns),
            trusted_product_context=trusted_product,
            max_tokens=s.route_call_max_tokens,
        )
        if context_failures:
            state.route_shadow.audit["context_failures"] = context_failures
        if state.route_shadow.error is not None:
            log.warning(
                "qa_route_shadow_failed",
                outcome=state.route_shadow.audit.get("outcome"),
                error_type=type(state.route_shadow.error).__name__,
            )

    routed = _pre_retrieval_route(
        state,
        question=question,
        session_id=session_id,
        s=s,
        _decline=_decline,
        _session_filters=_session_filters,
        _recent_turns=_recent_turns,
    )
    if not isinstance(routed, TurnState):
        return routed

    _finalize_route_shadow(reason="retrieval", response_mode=state.response_mode)

    grouped = _retrieve_and_group(
        state,
        question=question,
        k=k,
        s=s,
        _decline=_decline,
        _emit=_emit,
        _recent_turns=_recent_turns,
    )
    if not isinstance(grouped, TurnState):
        return grouped

    return _synthesize_and_admit(
        state,
        question=question,
        session_id=session_id,
        turn_id=turn_id,
        user_id=user_id,
        s=s,
        _decline=_decline,
        _emit=_emit,
        _recent_turns=_recent_turns,
        _emit_draft=on_draft,
        _emit_draft_reset=on_draft_reset,
    )


def ask(
    question: str,
    *,
    filters: dict[str, Any] | None = None,
    k: int | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    turn_id: str | None = None,
    bind_session: bool = True,
    origin: str = SESSION_ORIGIN_THREAD,
    on_progress: Callable[[str], None] | None = None,
    on_token: Callable[[str], None] | None = None,
    on_draft: Callable[[str], None] | None = None,
    on_draft_reset: Callable[[], None] | None = None,
) -> QAResult:
    """Grounded Q&A entry point: answer with citations, clarify, or decline.

    The thin persistence SHELL around ``ask_core``: it owns the session ids and
    every write (user message, audit row, assistant message, filter carry-over)
    while the core owns every decision. A future control plane replaces this
    function without touching the core.

    ``bind_session=False`` keeps ``user_id`` as audit-only attribution (INV-6):
    the bookkeeping ChatSession stays unowned (user_id NULL) and so invisible
    to /sessions. That is for internal callers like the dossier, whose synthetic
    Q&A must not appear in the caller's chat history.

    ``origin`` (default ``"thread"``) forwards to ``ensure_session`` and lands
    ONLY when the session row is newly CREATED this call -- an existing
    session's origin never changes underneath it (issue #208). Passing
    ``"assistant"`` means this conversation is kept (readable, deletable) but
    stays out of the work rail's Threads list -- the Research Studio panel's
    own scratch conversation, as opposed to the analyst's real work.

    ``on_progress`` (optional) receives short, cosmetic phase strings as the
    pipeline advances, for a live status ticker (POST /query/stream). It carries
    NO answer text or citations (INV-1 lives entirely in the post-validation
    answer path), and a failing sink can never break or slow the query.

    ``on_token`` (optional) receives the FINAL answer text in chunks, for a live
    "typing" effect. It fires only after the audit row is committed and only on
    an answer/summary turn, so every byte it emits is gated, rendered and
    audited: a declined or retracted draft can never reach it. A missing sink
    changes nothing else about the turn.

    ``on_draft`` / ``on_draft_reset`` (optional) receive LIVE, un-gated,
    provisional prose deltas (and retroactive discard signals) during
    synthesis, prose mode only -- the dual-gated draft channel under the
    owner-amended INV-1 (2026-08-10). Never validated, never replayed on
    declines the way on_token is; the terminal result stays authoritative.
    """
    # Touch settings/model-name BEFORE any write, matching pre-split ask(): both
    # are lru_cache-backed (near-free on every call after the first) but a first-
    # ever misconfiguration (e.g. a Settings validation error) must fail BEFORE
    # the user-message write below, not leave an orphaned question with no
    # assistant/audit response. ask_core re-reads them (cached, not re-fetched).
    get_settings()
    current_model_name(role="synthesizer")

    # Turn clock. Stamped before the user-message write, matching Go's t0
    # (query.go, before persistUserTurn) so relay-path and native-path
    # latency_ms measure the same interval and are comparable in one percentile.
    t0 = perf_counter()

    # Session bookkeeping is best-effort: a DB hiccup here must never stop the query
    # from being processed and audited (INV-6). Degrade to a fresh id on failure.
    # The user-message write stays HERE, before compute, so a core exception still
    # leaves the question in the chat history exactly as before the split.
    try:
        session_id = ensure_session(
            session_id, user_id=user_id if bind_session else None, origin=origin
        )
        turn_id = turn_id or new_turn_id()
        record_message(
            session_id=session_id,
            turn_id=turn_id,
            role="user",
            content=question,
            filters=filters,
        )
    except SessionOwnershipError:
        # Lost an ownership race after the API's pre-check. Abort rather than
        # write this caller's turns into another user's session (the API maps
        # this to its ownership 404).
        raise
    except SessionOriginError:
        # An origin outside SESSION_ORIGINS: a programming error in the caller
        # (the API boundary already validates), not the transient DB hiccup the
        # generic degrade below exists for. Propagate -- folding it into a
        # fresh-id degrade would hide the bad call instead of surfacing it.
        # Caught by its own type, not by ValueError: the try also wraps the
        # record_message write, and re-raising every ValueError from there
        # would narrow that degrade path for reasons unrelated to origin.
        raise
    except Exception:
        log.warning("session_setup_failed", exc_info=True)
        turn_id = turn_id or new_turn_id()
        # Degrade to a FRESH id, never the requested one: after a failed bind
        # (e.g. a lost create race on a client-chosen id) the requested session
        # may belong to someone else, so later writes must not target it.
        session_id = turn_id

    # Session context enters the core through loaders the SHELL owns (the core
    # never touches conversation storage). The loader bodies resolve
    # get_session_filters/get_recent_turns as module globals at CALL time so
    # tests that monkeypatch them on this module keep working; a future HTTP
    # shell passes constants over pre-loaded request data instead.
    sid, tid = session_id, turn_id
    try:
        outcome, audit, patch = ask_core(
            question,
            session_id=sid,
            turn_id=tid,
            filters=filters,
            k=k,
            user_id=user_id,
            load_session_filters=lambda: get_session_filters(sid),
            load_recent_turns=lambda: get_recent_turns(sid, limit=3, exclude_turn_id=tid),
            on_progress=on_progress,
            on_draft=on_draft,
            on_draft_reset=on_draft_reset,
        )
    except Exception as exc:
        # The SAME audited-error boundary compute_turn owns for the Go control
        # plane. The surfaces that reach ask() -- POST /query/stream (never
        # served natively) and POST /query whenever GO_NATIVE_QUERY is false
        # (the code default and the rollback path) -- must fail IDENTICALLY to
        # the native one: a raise in retrieve()/rerank/resolve otherwise leaves
        # the user turn recorded above with no audit row at all (INV-6).
        log.warning("qa_pipeline_error", error=str(exc), error_type=type(exc).__name__)
        capture_exception(exc)
        outcome, audit, patch = _pipeline_error(
            question,
            session_id=sid,
            turn_id=tid,
            user_id=user_id,
            filters=filters,
        )
    return _persist_turn(outcome, audit, patch, t0, on_token)


def _pipeline_error(
    question: str,
    *,
    session_id: str,
    turn_id: str,
    user_id: str | None,
    filters: dict[str, Any] | None,
) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
    """The DEFINED result of an unexpected pipeline crash: a fixed-copy
    status="error" refusal turn, skip-audited (allow_skip stays the AuditPayload
    default). Closes the INV-6 gap ``ask()`` left open -- a raise in
    retrieve()/rerank/resolve now yields a row to write, never a naked 500. Reuses
    ``_refuse`` + ``_build_patch`` so the audit/result/patch stay single-source.
    """
    active_filters = dict(filters or {})
    route_json = _route_json(
        filters=active_filters,
        reason="pipeline_error",
        context_applied=False,
        response_mode="refused",
    )
    outcome, audit = _refuse(
        question=question,
        passages=[],
        reason="pipeline_error",
        model_name=current_model_name(role="synthesizer"),
        session_id=session_id,
        turn_id=turn_id,
        user_id=user_id,
        route_json=route_json,
        status="error",
        answer_text=_SERVICE_UNAVAILABLE_TEXT,
    )
    return outcome, audit, _build_patch(outcome, filters=active_filters, route_json=route_json)


def compute_turn(
    question: str,
    *,
    filters: dict[str, Any] | None = None,
    k: int | None = None,
    session_id: str,
    turn_id: str,
    user_id: str | None = None,
) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
    """The stateless COMPUTE half of a turn, for the step-5 Go control plane.

    Runs ``ask_core`` behind the shell-owned session-context loaders -- the same
    call ``ask()`` makes -- but performs NO writes and adds the AUDITED-ERROR
    BOUNDARY: any unexpected raise in the pipeline (the retrieve/resolver gap
    ``ask()`` left unaudited) is caught here and turned into a DEFINED
    status="error"/pipeline_error outcome, so the caller (the Go CompleteQuery
    handler over POST /internal/query/compute) always receives a row to persist.
    Buffered path only; streaming stays in ``ask()`` (R3), so no on_progress/
    on_token sinks. ``session_id``/``turn_id`` are the caller's already-minted
    ids; reads (retrieval, resolver, session context) are allowed.
    """
    get_settings()
    current_model_name(role="synthesizer")
    try:
        return ask_core(
            question,
            session_id=session_id,
            turn_id=turn_id,
            filters=filters,
            k=k,
            user_id=user_id,
            load_session_filters=lambda: get_session_filters(session_id),
            load_recent_turns=lambda: get_recent_turns(
                session_id, limit=3, exclude_turn_id=turn_id
            ),
        )
    except Exception as exc:
        log.warning("qa_pipeline_error", error=str(exc), error_type=type(exc).__name__)
        capture_exception(exc)
        return _pipeline_error(
            question,
            session_id=session_id,
            turn_id=turn_id,
            user_id=user_id,
            filters=filters,
        )
