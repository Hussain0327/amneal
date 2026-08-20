"""Grounded Q&A orchestration.

Last updated: 2026-08-19.

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from time import perf_counter
from typing import Any, TypedDict, cast

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
from regwatch.common.stage_timing import TurnTimings, collect_stage_timings, stage
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
from regwatch.generate.turn_gate import (
    AdmittedTurn,
    GateFailure,
    admit_claims,
    admit_turn,
)
from regwatch.generate.turn_schema import TURN_SCHEMA_MESSAGE
from regwatch.generate.unresolved import classify_unresolved, is_social
from regwatch.retrieve.diversity import mmr_select
from regwatch.retrieve.mode import RetrievalPlan, RetrievalScope, default_mode_for_scope
from regwatch.retrieve.reranker import rerank_passages
from regwatch.retrieve.resolver import (
    lookup_external_drug,
    resolve_product,
    suggest_products,
)
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

# Synthesis temperature. A constant, not a setting: determinism is an invariant
# here, not an operator knob. The output cap is the operator knob
# (SYNTHESIZER_MAX_TOKENS) and ask_core reads it ONCE per turn.
_SYNTH_TEMPERATURE = 0.0

# Hard ceiling on a single synthesis call, INDEPENDENT of the setting. Defined
# in config.settings next to the setting it bounds, so the field validator can
# refuse a budget at or above it at boot; re-exported under the private name
# because callers and tests reference qa_mod._SYNTH_MAX_TOKENS_CEILING.
_SYNTH_MAX_TOKENS_CEILING = SYNTH_MAX_TOKENS_CEILING


class RetrievalBlock(TypedDict, total=False):
    """The stage-1 search ledger persisted under ``route_json["retrieval"]``.

    Empty means the turn ended before search ran; populated records what ran.
    "Did we search?" is a VALUE, never key presence: the same reason declines on
    BOTH sides of retrieve() (multi_form fires at the pre-retrieval form guard
    and again at the post-retrieval blend backstop), so presence could not
    answer it.
    """

    mode: str
    scope: str
    profile_id: str
    dimension: int
    k: int
    returned: int
    threshold: float


class RouteJson(TypedDict, total=False):
    """The audited route record for one turn.

    Read by the Go control plane and by every ops query over ``query_log``, so
    the key set is a contract. It is declared here rather than asserted in a
    comment: a branch that invents a key now fails type check instead of
    silently widening the column's shape.
    """

    route: str
    filters: dict[str, Any]
    reason: str
    context_applied: bool
    response_mode: str
    retrieval: RetrievalBlock
    retrieval_query_source: str
    retrieval_query_rewritten: bool
    prompt: dict[str, Any]
    guidance: dict[str, Any]
    synthesis: dict[str, Any]
    turn: dict[str, Any]
    gate_failure: dict[str, Any]
    partial_evidence: bool
    route_call: dict[str, Any]


@contextmanager
def _best_effort(event: str, **fields: Any) -> Iterator[None]:
    """Runs a cosmetic side effect, swallowing whatever it raises.

    The policy, stated once: a sink whose only job is presentation must never
    fail a turn that is already audited and already answered. Progress tickers
    and the draft/reset sinks qualify.

    Deliberately NOT used on the correctness path. A guard that must hold (the
    dosage-form enumeration, the strict audit write) degrades explicitly to an
    audited status="error" turn instead, and a residency violation re-raises.

    Args:
        event: structlog event name recorded if the block raises.
        **fields: Extra structlog fields attached to that record.

    Yields:
        None. Control returns to the caller whether or not the block raised.
    """
    try:
        yield
    except Exception:
        log.debug(event, exc_info=True, **fields)


def _maybe_inject_fault(stage: str) -> None:
    """Raises in the named pipeline stage when the R1 contract suite asks (S24).

    Forces an UNEXPECTED raise so the harness can prove the step-5 CompleteQuery
    audited-error boundary (``compute_turn``) turns a retrieve/resolver crash
    into exactly one status="error" audit row instead of the naked unaudited 500
    ``ask()`` used to leak.

    Args:
        stage: The pipeline stage name to compare against the env var.

    Raises:
        RuntimeError: When the env var names this stage AND the same
            ``allow_test_providers`` boot guard as the echo/forced-refusal
            providers is set, so it is inert in production regardless of the
            env var.
    """
    if os.environ.get("REGWATCH_FAULT_INJECT", "").strip() != stage:
        return
    if not get_settings().allow_test_providers:
        return
    raise RuntimeError(f"injected {stage} fault (REGWATCH_FAULT_INJECT={stage})")


@dataclass
class QAResult:
    """The public result of one ``ask()`` turn."""

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
    # _FOLLOW_UP_PRONOUNS, so it never matched.
    #
    # Safe to widen ONLY because two guards land alongside them:
    #   * _resolve_and_carry_over computes the "did you mean"/brand candidates
    #     BEFORE the carry-over, so a question naming a DIFFERENT product breaks
    #     the chain instead of inheriting the session's drug;
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
# regulatory fact. False-negatives are SAFE: a missed meta phrase falls through
# to the normal grounded path, where facts get cited. False-positives are the
# danger (a drug question routed to the uncited meta answer), so the gate also
# carries a named-drug HARD VETO; these phrases stay tight as defense in depth.
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

# A meta phrase whose subject is "what changed / what's new" pulls the recent
# Watch digest into the answer; everything else describes corpus + watchlist.
_META_CHANGE_PHRASES = ("what changed", "changed", "what's new", "whats new", "new")

# Bare numeric markers ("[3]", "[1, 2]") in conversation memory. Stripped by
# _format_recent ALONGSIDE the pair-form strip: under v6 prose a stale [3] in a
# prior answer would read as a live pointer into THIS turn's passage numbering.
# Unconditional and plain-deletion on purpose -- v5 history contains no numeric
# markers, so the sub is a byte-level no-op there and the v5 prompt (and its
# eval) stays byte-identical.
_NUMERIC_MARKER_RE = re.compile(r"\[\s*\d+\s*(?:,\s*\d+\s*)*\]")

# The corrective turn appended for the ONE bounds repair (issue #183). Phrased
# as a normal follow-up rather than an error report: the model is being asked to
# say the same thing more concisely, not told it violated a schema. No character
# count is quoted -- a number invites the model to pad up to it.
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

# Fixed, non-LLM copy for the status="error" refusal family (provider transport
# failure, catalog read failure, audit write failure). One literal so every
# degrade path stays in sync with the tests that assert on it.
_SERVICE_UNAVAILABLE_TEXT = (
    "The answer service is temporarily unavailable. Your question was "
    "not answered — please try again in a moment."
)

# Fixed, non-LLM copy for the conversational outcomes. Server-owned like every
# other non-answer string here: the model never writes display prose, and a
# greeting must not cost a model call at all.
CONVERSE_GREETING_TEXT = "Hey! What can I help you with today?"
NEED_PRODUCT_GUIDANCE_TEXT = "Sure — which product are you asking about?"

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


def _looks_like_follow_up(question: str) -> bool:
    """Reports whether the question reads as a follow-up to a prior turn.

    Args:
        question: The user's literal question.

    Returns:
        True when it opens with a known drill-down prefix, or is short and
        carries a back-referring pronoun.
    """
    q = question.strip().lower()
    if any(q.startswith(prefix) for prefix in _FOLLOW_UP_PREFIXES):
        return True
    tokens = {t for t in re.split(r"[^a-z0-9]+", q) if t}
    return bool(tokens & _FOLLOW_UP_PRONOUNS) and len(tokens) <= 8


def _is_summary_request(question: str) -> bool:
    """Reports whether the question asks for a summary rather than a fact.

    Args:
        question: The user's literal question.

    Returns:
        True when any summary term appears as a whole token.
    """
    tokens = {t for t in re.split(r"[^a-z0-9]+", question.lower()) if t}
    return bool(tokens & _SUMMARY_TERMS)


def _is_scope_warning_request(question: str) -> bool:
    """Reports whether the question asks us to author a filing decision (INV-3).

    Args:
        question: The user's literal question.

    Returns:
        True when any closed-set scope phrase appears.
    """
    q = question.lower()
    return any(phrase in q for phrase in _SCOPE_WARNING_PHRASES)


def _is_meta_request(question: str) -> bool:
    """Reports whether the question is a closed-set "what does this system do".

    Phrase-match ONLY. No LLM judges intent (an LLM mis-call would be the exact
    fabrication breach). A True here is necessary but NOT sufficient to route to
    the uncited meta path: the caller additionally vetoes any question that
    resolves to a named in-corpus drug, so "what BE study do you cover for
    atorvastatin?" never reaches ``_meta``.

    Args:
        question: The user's literal question.

    Returns:
        True when any closed-set meta phrase appears.
    """
    q = question.lower()
    return any(phrase in q for phrase in _META_PHRASES)


def _is_change_request(question: str) -> bool:
    """Reports whether a meta question is specifically about recent changes.

    Args:
        question: The user's literal question.

    Returns:
        True when the question names change or novelty.
    """
    q = question.lower()
    return any(phrase in q for phrase in _META_CHANGE_PHRASES)


def _audit_retrieved(passages: list[RetrievedPassage]) -> list[dict[str, Any]]:
    """Projects retrieved passages down to the audit trail's columns.

    Args:
        passages: Every passage retrieved this turn, sub-threshold included.

    Returns:
        One dict per passage carrying identity and score, never passage text.
    """
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
    """Resolves each citation's revision date BEFORE the turn is persisted.

    This join used to run only on the response path (api.main._wire_citations),
    so ``recommended_date`` never reached ``citations_json`` and every reopened
    conversation rendered "Revision date not recorded" forever. Running it here
    puts the version-correct date on the domain citation, which is what
    ``_build_patch`` serializes.

    One batched lookup for the whole turn (no N+1), and ``fetch_citation_recency``
    already swallows every exception to an empty index behind the connection's
    statement_timeout, so a failed or slow lookup yields null dates and an answer
    that still ships.

    Args:
        citations: The gate-validated citations for this turn.

    Returns:
        The same citations with ``recommended_date`` and ``diff_summary``
        resolved, in the same order.
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
) -> RouteJson:
    """Builds the base audit route record for a turn.

    Args:
        filters: The retrieval filters in force when the record is built.
        reason: Why the turn took the branch it did (e.g. "multi_form").
        context_applied: Whether session carry-over supplied any filter.
        response_mode: The status the turn is heading for.

    Returns:
        A RouteJson whose ``retrieval`` ledger is empty. Callers that reach
        stage-1 search overwrite it.
    """
    return {
        "route": "psg_scoped_rag",
        "filters": dict(filters),
        "reason": reason,
        "context_applied": context_applied,
        "response_mode": response_mode,
        "retrieval": {},
    }


def _build_patch(
    outcome: RagOutcome,
    *,
    filters: dict[str, Any],
    route_json: RouteJson,
) -> SessionPatch:
    """Describes the chat-history mutations this turn implies.

    Pure: mirrors what the shell's assistant-message write needs, with the
    audit_id deliberately absent (the audit row does not exist yet; the shell
    injects it in ``_apply_session_patch`` after logging).

    Args:
        outcome: The computed turn.
        filters: The filters to persist as the session's sticky scope.
        route_json: The audit route to embed in the message metadata.

    Returns:
        A SessionPatch the shell can apply verbatim.
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
        metadata={"retrieved": outcome.retrieved, "route": dict(route_json)},
        update_filters=bool(
            outcome.status in {"answer", "summary", "clarify"} and filters.get("normalized_name")
        ),
    )


def _apply_session_patch(patch: SessionPatch, *, audit_id: int) -> None:
    """Applies the chat-history writes a completed turn implies.

    Best effort by design: the audit row (INV-6) is already committed by this
    point, so a failure here must not 500 an already-audited turn. The degraded
    session_id=turn_id fallback has no chat_session row, so the assistant FK
    insert is an expected failure mode.

    Args:
        patch: The mutations computed by the core.
        audit_id: The id of the audit row this turn already wrote.
    """
    if not (patch.session_id and patch.turn_id):
        return
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
    """Reports whether the question is a bare drug name plus filler.

    Deterministic (no LLM), so the "bare drug name -> clarify" hero path is
    unit-testable; the live ``model_refusal`` net handles everything subtler.

    Args:
        question: The user's literal question.
        normalized_name: The resolved product, whose tokens do not count.

    Returns:
        True when nothing but the drug name and filler survives.
    """
    drug_tokens = {t for t in re.split(r"[^a-z0-9]+", normalized_name.lower()) if t}
    residual = [
        t
        for t in re.split(r"[^a-z0-9]+", question.lower())
        if t and t not in drug_tokens and t not in _FILLER
    ]
    return not residual


def _carries_own_topic(question: str, normalized_name: str | None) -> bool:
    """Reports whether the question has a term worth embedding on its own.

    Deliberately NOT ``not _looks_vague(...)``. The vague gate asks "is there
    anything here besides the drug name and filler", and "why" is neither, so it
    reads as a topic. For RETRIEVAL the question is different: "why" is pure
    discourse and embeds to nothing useful. _DRILL_DOWN_WORDS is that difference,
    meta vocabulary that is never an FDA-guidance retrieval term in this corpus.

    Conservative on purpose: any survivor counts as a topic, so "what about
    dissolution?" keeps its own embedding and today's working follow-ups are
    untouched. The cost is that some phrasings ("what if the study fails?") are
    not re-anchored; those behave exactly as they do today.

    Args:
        question: The user's literal question.
        normalized_name: The resolved product, if any; its tokens do not count.

    Returns:
        True when at least one non-filler, non-discourse token survives.
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
    """Chooses the text to EMBED for this turn, not necessarily the user's words.

    A drill-down follow-up ("why?", "tell me more") carries no topical signal.
    Embedded verbatim it scores near-zero against every passage, so the turn dies
    on the ``low_top_score`` refusal even though the session context makes the
    intent obvious. Re-anchor it on the most recent prior QUESTION so the vector
    search sees the subject the user is still asking about.

    RETRIEVAL ONLY. The synthesizer still receives the user's literal question
    plus the conversation block, so the answer addresses what was actually asked.
    INV-1 is untouched either way: the gate validates every citation against THIS
    turn's passages, so how a passage was FOUND cannot make an unsupported claim
    citable.

    Args:
        question: The user's literal question.
        normalized_name: The resolved product, if any.
        prior_turns: Conversation memory, oldest first.

    Returns:
        The re-anchored query, or the raw question whenever the rewrite would be
        a guess (no follow-up shape, the question has its own topic, or there is
        no prior turn to anchor on).
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
    """Counts the PSG documents on file for one product.

    Args:
        normalized_name: The canonical product key.

    Returns:
        The document count, or 0 when the product has none.
    """
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(PsgDocument)
                .where(PsgDocument.normalized_name == normalized_name)
            )
            or 0
        )


def _interpretation_for(normalized_name: str) -> str:
    """Writes the clarify copy naming what we hold for a resolved product.

    Args:
        normalized_name: The canonical product key.

    Returns:
        One sentence of server-owned display copy, no citations.
    """
    count = _doc_count(normalized_name)
    docs = "document" if count == 1 else "documents"
    have = (
        f"FDA has {count} product-specific guidance {docs} for it"
        if count
        else "I have its FDA guidance"
    )
    return f"You're asking about {normalized_name.title()}. {have} — what would you like to know?"


def build_options(normalized_name: str) -> list[ClarifyOption]:
    """Builds the plain-language things we can actually answer for a product.

    These are QUESTION TEMPLATES that re-run retrieval. They do not read the
    (possibly empty) BeRequirement table, so they work on the full catalog.

    Args:
        normalized_name: The canonical product key.

    Returns:
        Three re-runnable clarify options scoped to that product.
    """
    name = normalized_name
    filters = {"normalized_name": name}
    return [
        ClarifyOption(
            "Recommended bioequivalence (BE) study — how FDA wants a generic shown equivalent",
            f"What bioequivalence study design does FDA recommend for {name}?",
            filters,
        ),
        ClarifyOption(
            "Dissolution method",
            f"What dissolution method does the FDA guidance recommend for {name}?",
            filters,
        ),
        ClarifyOption(
            "Strengths and dosage forms covered",
            f"What strengths and dosage forms does the FDA guidance cover for {name}?",
            filters,
        ),
    ]


def _related_from_passages(passages: list[RetrievedPassage]) -> list[ClarifyOption]:
    """Builds "related, not an answer" pointers from the passages in hand.

    Surfaces DISTINCT product NAMES ONLY, never the passage text or score (chunk
    text would read as quasi-evidence on a refusal). Deduped by product name,
    first occurrence wins (retrieval order is best match first). Each option
    re-runs as a name-scoped query, so it renders as an inert, re-runnable pill,
    never a citation chip. Filters carry retrieval constraints only, so no
    display values belong here; the API boundary would strip them anyway.

    Args:
        passages: The sub-threshold passages that tripped the refusal.

    Returns:
        One inert option per distinct product, in retrieval order.
    """
    options: list[ClarifyOption] = []
    seen: set[str] = set()
    for passage in passages:
        name = (passage.normalized_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        options.append(ClarifyOption(name.title(), name, {"normalized_name": name}))
    return options


def _options_from_names(names: list[str]) -> list[ClarifyOption]:
    """Turns bare product names into re-runnable pills.

    THE one rule for how a product name becomes a clickable option: clarify
    choices (ambiguous / did-you-mean / brand / mixed-products) and the decline
    family's inert pointers all share it, so the two pill kinds can never render
    or behave differently for the same name. No DB hit.

    Args:
        names: Product names, already canonical.

    Returns:
        One option per name; an empty input yields an empty list.
    """
    return [ClarifyOption(name.title(), name, {"normalized_name": name}) for name in names]


def _combo_label(normalized_name: str, dosage_form: str, route: str) -> str:
    """Formats a human-readable combo label.

    Args:
        normalized_name: The canonical product key.
        dosage_form: The catalog dosage form.
        route: The catalog route of administration.

    Returns:
        A label such as ``Estradiol — Gel, Metered (Transdermal)``.
    """
    return f"{normalized_name.title()} — {dosage_form} ({route})"


def build_form_options(
    normalized_name: str, combos: list[tuple[str, str]], question: str
) -> list[ClarifyOption]:
    """Builds one clickable option per (dosage_form, route) combo.

    Each option re-runs the SAME question (so the user's intent survives the
    extra hop) but pins ``dosage_form`` and ``route`` alongside
    ``normalized_name`` so retrieval is constrained to a single form, which is
    what stops a wrong-form PSG being cited. Filters round-trip verbatim through
    the API and UI.

    Args:
        normalized_name: The canonical product key.
        combos: The distinct (dosage_form, route) pairs to offer.
        question: The question to re-run once a form is chosen.

    Returns:
        One option per combo, in the order given.
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
    """Extracts significant form/route tokens, dropping short connectors.

    Args:
        value: A dosage form or route string from the catalog.

    Returns:
        Lowercased tokens longer than two characters.
    """
    return {t for t in re.split(r"[^a-z0-9]+", value.lower()) if len(t) > 2}


def _combo_from_question(question: str, combos: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Pins one combo when the question names it unambiguously.

    Scores each combo by how many of its significant dosage_form/route tokens
    appear as whole words in the question, then returns the uniquely best match.
    So "albuterol sulfate inhalation aerosol" pins (Aerosol, Metered) /
    (Inhalation) instead of paying a pointless clarify hop, while a form-silent
    or ambiguous question still clarifies.

    Three corrections keep this honest on the oral-tablet mass (68% of catalog):
      * _FILLER is stripped from the question tokens first. Otherwise the
        3-letter stopword "for" (which survives the len>2 cut) collides with real
        catalog forms like "Tablet, For Suspension" and SILENTLY pins the wrong
        form: a wrong-form citation, the worst INV-1 outcome.
      * Ties on raw match count break toward the combo the question covers most
        COMPLETELY, so a plain "tablet" pins (Tablet) over its (Tablet, Extended
        Release) sibling, while "extended release tablet" still pins the ER
        variant and a token fitting ER and ODT equally still clarifies.
      * That completeness tie-break applies only when the question matched at
        least one DOSAGE-FORM token. A tie carried by shared ROUTE tokens alone
        names no form, so breaking it would silently pin an arbitrary one.

    Args:
        question: The user's literal question.
        combos: The candidate (dosage_form, route) pairs.

    Returns:
        The uniquely best-matching combo, or None when the caller should
        clarify.
    """
    q_tokens = {t for t in re.split(r"[^a-z0-9]+", question.lower()) if t and t not in _FILLER}

    def _score(form: str, route: str) -> tuple[tuple[int, int], int]:
        form_tokens = _form_match_tokens(form)
        combo_tokens = form_tokens | _form_match_tokens(route)
        matched = len(combo_tokens & q_tokens)
        # Primary: more matched tokens. Secondary: fewer of the combo's own
        # tokens left uncovered (negated so "more complete" sorts first). The
        # form-token match count rides ALONGSIDE the sort key, never inside it,
        # so an existing full-tuple tie stays a tie for the route-only guard.
        return (matched, -(len(combo_tokens) - matched)), len(form_tokens & q_tokens)

    scored = sorted(
        ((*_score(form, route), (form, route)) for form, route in combos),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, best_form_matched, best_combo = scored[0]
    if best_score[0] == 0:
        return None  # The question named no form at all, so clarify.
    if len(scored) > 1 and scored[1][0] == best_score:
        return None  # Two combos fit equally well (match AND completeness).
    if best_form_matched == 0 and len(scored) > 1 and scored[1][0][0] == best_score[0]:
        # The win came ONLY from the completeness tie-break over shared route
        # tokens: the question named no dosage form, so a pin here would be the
        # silent wrong-form pin this function exists to prevent.
        return None
    return best_combo


def _format_passages(passages: list[RetrievedPassage]) -> str:
    """Renders passages for the v5 prompt, with pair-form headers.

    Args:
        passages: The above-threshold evidence for this turn.

    Returns:
        The prompt block, passages separated by a rule.
    """
    blocks: list[str] = []
    for passage in passages:
        section = f" ({passage.section_path})" if passage.section_path else ""
        blocks.append(
            f"[{passage.short_name}, p.{passage.page}]{section}\n{passage.text.strip()}\n"
        )
    return "\n---\n".join(blocks)


def _format_passages_numbered(passages: list[RetrievedPassage]) -> str:
    """Renders passages for the v6/v7 prompt, prefixing each with a [n] label.

    The [n] label is what the model cites; the [SHORT_NAME, p.N] pair header
    stays because the prose parser's pair-echo collision handling and the echo
    provider's discriminating scrape both key on it, and an operator reading a
    raw prompt still sees the canonical citation identity next to each number.

    Args:
        passages: The above-threshold evidence for this turn.

    Returns:
        The prompt block, passages separated by a rule.
    """
    blocks: list[str] = []
    for position, passage in enumerate(passages, start=1):
        section = f" ({passage.section_path})" if passage.section_path else ""
        blocks.append(
            f"[{position}] [{passage.short_name}, p.{passage.page}]{section}\n"
            f"{passage.text.strip()}\n"
        )
    return "\n---\n".join(blocks)


def _format_recent(turns: list[PriorTurn]) -> str:
    """Renders prior turns as a compact, citation-free conversation block.

    Citations are stripped so the model cannot see, and therefore cannot parrot,
    a stale ``[PSG, p.N]`` whose page may not be in THIS turn's passages, and
    each side is capped so the current passages stay dominant in the window.
    Reference-only, never evidence (INV-1): the system prompt forbids treating it
    as a source, and the gate accepts only markers grounded in this turn's
    passages regardless.

    Args:
        turns: Conversation memory, oldest first.

    Returns:
        The context block, or "" when nothing usable survives (which keeps the
        prompt byte-identical to the single-turn form and protects the eval).
    """
    lines: list[str] = []
    for turn in turns:
        # Strip markers from BOTH sides: a citation-shaped token in a prior
        # question is context too, never a source the model may reuse this turn.
        question = _NUMERIC_MARKER_RE.sub("", strip_all_citations(turn.question)).strip()[:400]
        # Stored answers end with a "Sources:" trailer. Current entries are
        # bracketed, legacy entries were not; drop the whole trailer before the
        # bracket strip (shared with eval/metrics.faithfulness) so neither form
        # can become a stale re-citable pointer in conversation memory.
        answer_prose = strip_sources_trailer(turn.answer)
        answer = _NUMERIC_MARKER_RE.sub("", strip_all_citations(answer_prose)).strip()[:600]
        if not question and not answer:
            continue
        lines.append(f"User: {question}\nAssistant: {answer}")
    return "\n\n".join(lines)


def _complete_structured(
    provider: LLMProvider,
    messages: list[LLMMessage],
    *,
    max_tokens: int,
    response_format: str | None = "json",
    telemetry: dict[str, Any] | None = None,
) -> LLMResponse:
    """Issues one buffered completion with a single 2x truncation retry.

    Buffered, never ``provider.stream()``: stream() takes no response_format on
    the Protocol or in any implementation, ``_buffered_stream`` drops it, and the
    Databricks stream fallback re-issues through ``_buffered_stream``, so a
    schema-rejecting endpoint would silently hand unstructured PROSE back to a
    structured caller. The user-visible typing effect is preserved by replaying
    the RENDERED answer after the audit write (see ``_persist_turn``).

    Args:
        provider: The role-scoped LLM provider.
        messages: The full prompt.
        max_tokens: Requested output budget, clamped to the module ceiling.
        response_format: "json" for every legacy caller; None for the v6/v7 prose
            synthesizer, so no json_object mode and no appended user json
            directive reaches the wire.
        telemetry: Optional dict the caller keeps a reference to; the budget and
            any retry are recorded into it during the call.

    Returns:
        The terminal LLMResponse.

    Raises:
        D1ResidencyError: Re-raised FIRST. A residency violation must fail the
            turn loudly, never be retried against the endpoint the guard fences
            off and never degrade into a parse failure.
        RuntimeError: The payload was unusable and no larger budget exists.
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
        # so the retry can cost one extra call on a non-truncation fault. That is
        # bounded to one and never changes the outcome, and narrowing the
        # predicate would couple this module to provider message text; do it with
        # a typed exception in llm.py, not a substring match. Transport faults
        # (429/5xx/timeouts) are openai.APIError, NOT RuntimeError, so they skip
        # this branch and land on the audited provider_error path immediately.
        #
        # The retry budget is computed FIRST and speaks for itself: there is no
        # bigger budget to escalate to, and re-issuing a byte-identical request
        # at temperature 0.0 would only burn a call.
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
    on_delta: Callable[[str], None],
    on_reset: Callable[[], None],
    telemetry: dict[str, Any] | None = None,
) -> LLMResponse:
    """Streams one prose completion, forwarding scrubbed provisional deltas.

    The streaming twin of ``_complete_structured``, prose mode only. The
    parse/admit gate downstream always operates on the COMPLETE returned text,
    exactly as on the buffered path. The sentinel hold lives HERE,
    provider-agnostically (Echo streams too): no delta is forwarded while the
    accumulated text is still a prefix of the no-evidence sentinel, so a refusal
    never paints. Truncation keeps the same one-2x-retry policy; because attempt
    1's deltas may already be on the wire, the retry emits ``on_reset`` first so
    the client discards the partial draft, then re-streams.

    Args:
        provider: The role-scoped LLM provider.
        messages: The full prompt.
        max_tokens: Requested output budget, clamped to the module ceiling.
        on_delta: Sink for provisional, UN-GATED prose deltas.
        on_reset: Retroactive discard signal for whatever on_delta already sent.
        telemetry: Optional dict recording the budget and any retry.

    Returns:
        The terminal LLMResponse.

    Raises:
        D1ResidencyError: Re-raised first, exactly like the buffered twin.
        RuntimeError: The stream ended with no terminal chunk, or the payload was
            unusable and no larger budget exists.
    """
    capped = min(max_tokens, _SYNTH_MAX_TOKENS_CEILING)
    if telemetry is not None:
        telemetry["first_budget"] = capped

    def _attempt(budget: int) -> tuple[LLMResponse | None, bool]:
        """Runs one stream.

        Args:
            budget: The output budget for this attempt.

        Returns:
            A (terminal response or None, whether any delta was forwarded) pair.
        """
        held = ""
        holding = True
        forwarded = False

        def _forward(text: str) -> None:
            nonlocal forwarded
            if not text:
                return
            forwarded = True
            with _best_effort("on_draft_failed"):
                on_delta(text)

        response: LLMResponse | None = None
        for chunk in provider.stream(messages, temperature=_SYNTH_TEMPERATURE, max_tokens=budget):
            if chunk.reset:
                held, holding = "", True
                if forwarded:
                    with _best_effort("on_draft_reset_failed"):
                        on_reset()
                continue
            if chunk.done:
                response = chunk.response
                break
            if holding:
                held += chunk.delta
                if prose_turn.PROSE_NO_EVIDENCE_SENTINEL.startswith(held):
                    continue  # Still a possible refusal prefix; keep holding.
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
            "qa_synthesis_truncation_retry",
            old=capped,
            new=retry_budget,
            error=str(exc)[:200],
        )
        with _best_effort("on_draft_reset_failed"):
            on_reset()
        response, _ = _attempt(retry_budget)
    if response is None:
        raise RuntimeError("provider stream ended without a terminal response chunk")
    return response


def _gate_failure_class(detail: str) -> str:
    """Buckets a gate parse failure into a cause an operator can act on.

    ``malformed_structure`` fuses four unrelated faults: the model exceeded a
    schema cap, it emitted invalid JSON, it emitted nothing extractable, or it
    broke the schema some other way. The remedy differs for each (raise a cap /
    change decoding / check the endpoint / fix the prompt), and today they are
    indistinguishable in the DB, so the rate is uninterpretable.

    Substring matching is acceptable ONLY because every input string is produced
    by this repo's own turn_gate (the three GateFailure sites) or by pydantic's
    ValidationError json. It must never be pointed at provider text.

    Args:
        detail: The GateFailure detail string.

    Returns:
        One of json_decode, empty_extract, list_too_long, text_too_long, or
        schema_other.
    """
    normalized = detail.lower()
    if "json decode failed" in normalized:
        return "json_decode"
    if "empty response after extraction" in normalized:
        return "empty_extract"
    # Match pydantic's error type EXACTLY. A plain `"too_long" in detail` also
    # matches "string_too_long", fusing "the model wrote 21 claims" with "the
    # model wrote a 401-char claim" -- different faults with different fixes.
    if '"type":"too_long"' in normalized:
        return "list_too_long"
    if '"type":"string_too_long"' in normalized:
        return "text_too_long"
    return "schema_other"


def _gate_log_fields(admitted: AdmittedTurn) -> dict[str, Any]:
    """Builds operator counters for one gate decision.

    No claim text and no citations: the full per-claim record (text prefix,
    cites, drop reason, materiality word) is persisted in ``route_json["turn"]``.
    This line exists so the drop rate is greppable without a DB query.

    Args:
        admitted: The gate's decision.

    Returns:
        structlog fields for one ``qa_turn_gate`` record.
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
    """Splits a rendered answer into ~60-char whitespace-boundary chunks.

    Whitespace boundaries only: splitting mid-token could tear a citation marker
    across two frames, and a half-marker is exactly the shape a client would
    render as literal prose.

    Args:
        text: The rendered, gated answer.

    Returns:
        The chunks, in order, which concatenate back to ``text``.
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
    """Builds the log_query kwargs for a turn's synthesizer or guidance usage.

    Args:
        model_name: The model that produced the usage, for pricing.
        usage: The reported usage, or None when no LLM call happened.

    Returns:
        Token and cost kwargs. None keeps all three columns NULL; an unpriced
        model keeps cost_usd NULL while still recording the token counts.
    """
    if usage is None:
        return {}
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": estimate_cost_usd(model_name, usage),
    }


def _log_query_or_skip(**kwargs: Any) -> int:
    """Writes an audit row, returning -1 instead of raising when the write fails.

    Used only by the no-LLM-content terminal paths: their payload is fixed copy
    or system-state text with zero citations, so returning it without an audit
    row beats a naked, unaudited 500 that the stream-fallback client would re-run
    into the same down DB. The skip is logged and Sentry-captured.

    Args:
        **kwargs: Forwarded verbatim to ``log_query``.

    Returns:
        The new audit row id, or -1, which never collides with a real QueryLog
        id.
    """
    try:
        return log_query(**kwargs)
    except Exception as exc:
        log.warning("qa_audit_write_failed", error=str(exc), error_type=type(exc).__name__)
        capture_exception(exc)
        return -1


def _latency_ms(t0: float | None) -> int | None:
    """Measures whole-millisecond turn wall time. Go's ``latencyMs`` twin.

    Args:
        t0: The shell's ``perf_counter`` start stamp, or None.

    Returns:
        The elapsed milliseconds, or None when no start stamp was taken. None,
        never 0: a percentile over a column where "unknown" and "instant" both
        read 0 understates the provider-cutover gates that consume it. The clamp
        is an int4 column-width guard, not a reachable path.
    """
    if t0 is None:
        return None
    return min(int((perf_counter() - t0) * 1000), 2**31 - 1)


def _attach_stage_timings(audit: AuditPayload, timings: TurnTimings) -> None:
    """Folds one turn's stage timings into the audit row's ``route_json``.

    Rides ``route_json`` rather than a new column: it is already the row's
    diagnostic bag, already JSON, and needs no migration. Present on EVERY
    Python-authored row (an early-branch turn reports zero) so the pinned
    cross-runtime key set stays deterministic; rows Go synthesizes without
    reaching Python correctly carry no block at all.

    Never raises: a diagnostic must not be able to fail a turn that otherwise
    succeeded, least of all on the INV-6 answer path where a lost audit row
    costs the user their answer.
    """
    try:
        audit.route_json["timings"] = timings.as_route_json()
    except Exception:  # broad: instrumentation is never worth failing a turn
        log.debug("stage_timings_attach_failed", exc_info=True)


def _persist_turn(
    outcome: RagOutcome,
    audit: AuditPayload,
    patch: SessionPatch,
    t0: float | None = None,
    on_token: Callable[[str], None] | None = None,
) -> QAResult:
    """Performs the write half of a turn: audit row FIRST, then chat history.

    Everything user-visible is decided by the pure core; this function only
    performs the writes the core described and injects the audit_id they share.

    Args:
        outcome: The computed turn.
        audit: The audit row to write, including its strict-write fallback.
        patch: The chat-history mutations to apply afterwards.
        t0: The shell's turn clock. Latency is stamped HERE rather than supplied
            by the core because the core is stateless and cannot see transport
            time; Go's ``auditParams`` makes the same split.
        on_token: Replays the RENDERED, gated answer AFTER the audit write
            succeeds, and only on an answer/summary turn. Deliberately not where
            it used to live: streaming provisional model tokens from inside the
            core meant a user could read text the gate later retracted, and meant
            a complete answer could reach the reader with no audit row anywhere
            (INV-6). The cost is time-to-first-token; the compensation is that no
            byte a user sees is ever un-audited or un-gated.

    Returns:
        The public QAResult, carrying the audit row's id.

    Raises:
        Exception: Only when a strict audit write fails AND the payload carries
            no fallback, which strict payloads always do.
    """
    log_kwargs = {**audit.log_kwargs(), "latency_ms": _latency_ms(t0)}
    if audit.allow_skip:
        audit_id = _log_query_or_skip(**log_kwargs)
    else:
        try:
            audit_id = log_query(**log_kwargs)
        except Exception as exc:
            # No-audit-no-answer (INV-6): a validated answer with no audit row is
            # never returned, but the failure must be DEFINED, not a naked 500
            # that the stream-fallback client re-runs (a second paid synthesis)
            # into the same down DB. Degrade to the core-supplied fixed-copy
            # status="error" refusal turn, whose own audit is re-attempted and
            # skipped (flagged) if the DB is still down.
            log.warning(
                "qa_answer_audit_write_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            capture_exception(exc)
            if audit.failure_fallback is None:  # Defensive: strict payloads carry one.
                raise
            fb_outcome, fb_audit, fb_patch = audit.failure_fallback
            # Same t0: the fallback row records how long THIS turn took, not how
            # long the retry after a failed audit write took.
            return _persist_turn(fb_outcome, fb_audit, fb_patch, t0, on_token)

    # Terminal-decline log lines, emitted after the audit write exactly as the
    # pre-split makers did (each status maps to one maker, so the event names
    # cannot drift). The answer/summary path never logged here.
    if outcome.status == "clarify":
        log.info(
            "qa_clarify",
            reason=outcome.reason,
            audit_id=audit_id,
            options=len(outcome.clarify),
        )
    elif outcome.status == "meta":
        log.info("qa_meta", audit_id=audit_id)
    elif outcome.refused:
        log.info("qa_refused", reason=outcome.reason, audit_id=audit_id)

    # The typing effect, rebuilt on the safe side of the write. The status filter
    # is the whole guard: a decline, a clarify or an error replays NOTHING, so a
    # retracted draft can never be painted for a beat and then vanish.
    if on_token is not None and outcome.status in ("answer", "summary"):
        for chunk in _replay_chunks(outcome.answer):
            try:
                on_token(chunk)
            except Exception:
                # Stop replaying rather than hammer a sink that just failed. The
                # turn is audited and the authoritative answer rides the terminal
                # result frame regardless.
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
    route_json: RouteJson,
    status: QueryStatusLiteral = "refused",
    answer_text: str | None = None,
    usage: LLMUsage | None = None,
    related: list[ClarifyOption] | None = None,
) -> tuple[RagOutcome, AuditPayload]:
    """Describes a declined turn: zero citations, one audit row (INV-6).

    Args:
        question: The user's literal question.
        passages: Everything retrieved, kept for forensics even on a decline.
        reason: The machine-readable decline reason.
        model_name: The model to attribute the turn to.
        session_id: The shell's session id.
        turn_id: The shell's turn id.
        user_id: Audit attribution only.
        route_json: The audit route for this branch.
        status: The wire status; "error" for the service-unavailable family.
        answer_text: Server-owned copy, or None to serve the configured refusal
            text.
        usage: Usage from any LLM call that already happened this turn.
        related: Inert "related, not an answer" pointers. Never changes the
            refusal contract.

    Returns:
        The (outcome, audit) pair for this decline.
    """
    settings = get_settings()
    answer = answer_text or settings.refusal_text
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
        route_json=dict(route_json),
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
    route_json: RouteJson,
    usage: LLMUsage | None = None,
    related: list[ClarifyOption] | None = None,
    passages: list[RetrievedPassage] | None = None,
) -> tuple[RagOutcome, AuditPayload]:
    """Describes a guide-instead-of-guess turn: we know the product, not the ask.

    Carries ZERO citations (never fabricates) and describes one audit row
    (INV-6), exactly like ``_refuse``.

    Args:
        question: The user's literal question.
        reason: The machine-readable clarify reason.
        model_name: The model to attribute the turn to.
        interpretation: The server-owned display copy.
        options: The re-runnable choices offered.
        session_id: The shell's session id.
        turn_id: The shell's turn id.
        user_id: Audit attribution only.
        route_json: The audit route for this branch.
        usage: Usage from any LLM call that already happened this turn.
        related: Inert pointers offered alongside the options.
        passages: Set only by the POST-retrieval defense-in-depth clarifies
            (mixed_products / multi_form backstop) so the audit row keeps the
            evidence that tripped the guard, exactly the turns where forensics
            matter. Pre-retrieval clarifies leave it None.

    Returns:
        The (outcome, audit) pair for this clarify.
    """
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
        route_json=dict(route_json),
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
    route_json: RouteJson,
    filters: dict[str, Any] | None = None,
) -> tuple[RagOutcome, AuditPayload]:
    """Declines to author a filing decision (INV-3), naming what we CAN cite.

    The decline itself never changes. But if the question is ABOUT a real product
    we name the in-scope, citable sub-questions and hand back re-runnable
    pointers so the user has a next step instead of a dead end.

    Args:
        question: The user's literal question.
        reason: The machine-readable decline reason.
        model_name: The model to attribute the turn to.
        session_id: The shell's session id.
        turn_id: The shell's turn id.
        user_id: Audit attribution only.
        route_json: The audit route for this branch.
        filters: Caller-pinned filters; a ``normalized_name`` here short-circuits
            resolution.

    Returns:
        The (outcome, audit) pair, with status="scope_warning".
    """
    generic = (
        "I can help summarize and answer questions from FDA sources, but I cannot "
        "author submission strategy, recommend what to file, or make a regulatory "
        "judgment. If you name the product and source area, I can look up the FDA "
        "evidence and cite what the records say."
    )
    name = (filters or {}).get("normalized_name")
    if not name:
        # Resolution hits the vector store, so it can raise or time out. A
        # resolver failure must NOT break the refusal, so fall back to generic.
        try:
            resolution = resolve_product(question)
            if resolution.status == "resolved" and resolution.normalized_name:
                name = resolution.normalized_name
        except Exception:
            log.warning("scope_warning_resolve_failed", exc_info=True)
            name = None

    if name:
        options = build_options(str(name))
        product = str(name).title()
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


def _meta_answer_text(question: str) -> str:
    """Assembles a meta answer from VERIFIED SYSTEM STATE ONLY, never an LLM.

    Three independent system facts, each read live and clearly labeled so they
    are NEVER conflated: the askable PSG corpus, the products Watch monitors,
    and (only for a "what changed" phrasing) the most recent durable alerts.
    Carries no passage text and no citations, so it cannot emit a regulatory
    claim.

    Args:
        question: The user's literal question, read only to decide whether the
            change digest belongs in the answer.

    Returns:
        The assembled display copy.
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
            # NON-PROSE system facts ONLY: product name plus capture date. The
            # alert's diff_summary/rationale are LLM output or raw PSG passage
            # text (see process/change_detector.summarize_change). That is a
            # regulatory claim, not a system fact, so it must NEVER reach this
            # uncited meta answer (INV-1). Detail lives on the cited Watch feed.
            change_bits = []
            for record in records:
                name = str(record.get("active_ingredient") or "").strip().title() or "a product"
                # captured_at is an ISO timestamp; keep only the date (system
                # bookkeeping, never regulatory prose). Tolerate odd shapes.
                date = str(record.get("captured_at") or "").strip()[:10]
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
    route_json: RouteJson,
) -> tuple[RagOutcome, AuditPayload]:
    """Answers a "what does this system do" question from system state only.

    Mirrors ``_scope_warning`` as an application-owned handler: one audit row
    (INV-6), zero citations, and no model-authored prose. The surrounding
    ``_decline`` ceremony still gives the valid turn one bounded guidance call.
    This is NOT a refusal: ``refused`` is False and ``status`` is "meta".

    Args:
        question: The user's literal question.
        reason: The machine-readable route reason.
        model_name: The model to attribute the turn to.
        session_id: The shell's session id.
        turn_id: The shell's turn id.
        user_id: Audit attribution only.
        route_json: The audit route for this branch.

    Returns:
        The (outcome, audit) pair, structurally citation- and
        fabrication-incapable.
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
        route_json=dict(route_json),
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
    route_json: RouteJson,
) -> tuple[RagOutcome, AuditPayload]:
    """Answers a social turn as a person would, with no machinery behind it.

    A greeting is not a failed product lookup. It reaches no resolver, no
    retrieval and no model: the copy is fixed and server-owned, so this handler
    is citation- and fabrication-incapable by construction, exactly like
    ``_meta``, whose status it deliberately shares. ``status="meta"`` keeps the
    seven-value ``QueryStatusLiteral`` (and the Go proxy, the SSE grammar, the
    persisted rows and the frontend allowlist that mirror it) untouched, and it
    is the status a promoted ``mode=converse`` turn will land on too.

    Args:
        question: The user's literal question.
        reason: The machine-readable route reason.
        model_name: The model to attribute the turn to.
        session_id: The shell's session id.
        turn_id: The shell's turn id.
        user_id: Audit attribution only.
        route_json: The audit route for this branch.

    Returns:
        The (outcome, audit) pair. ``refused`` is False, so the frontend renders
        plain prose with no declined register and no diagnostic reason line.
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
        route_json=dict(route_json),
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
    """The mid-flow mutable state of one ``ask_core`` turn, made explicit.

    One instance per turn, created by ``ask_core`` and threaded through the stage
    functions below; each stage mutates it in place and returns it, or returns a
    terminal decline triple. The ``_decline`` ceremony reads ``active_filters``
    and ``context_applied`` from here at CALL time. Before the extraction these
    were loose closure variables, so that read-at-call-time contract was
    invisible in any signature.
    """

    active_filters: dict[str, Any]
    context_applied: bool = False
    resolved_by_name: bool = False
    response_mode: QueryStatusLiteral = "answer"
    # Filled by _retrieve_and_group for _synthesize_and_admit: every retrieved
    # row (the audit trail), the individually above-threshold subset (all the
    # synthesizer may see), and the retrieval route the answer path's audit row
    # carries.
    passages: list[RetrievedPassage] = field(default_factory=list)
    evidence_passages: list[RetrievedPassage] = field(default_factory=list)
    route_json: RouteJson = field(default_factory=RouteJson)
    # Populated once stage-1 search runs; read by _decline so a turn that
    # declines AFTER retrieving still records which retrieval mode ran.
    retrieval_block: dict[str, Any] = field(default_factory=dict)
    # PR11b observation only. The model result cannot steer any field above; its
    # finalized JSON is copied into the audit route immediately before the
    # existing path returns or retrieves.
    route_shadow: RouteShadowObservation | None = None
    route_shadow_audit: dict[str, Any] | None = None
    # PR12, live mode only: the compiled scope that actually steered this turn's
    # pre-retrieval path (set only from _resolve_and_carry_over's no-product
    # branch), and the standalone rewrite _retrieve_and_group should search with
    # instead of _retrieval_query's heuristic rewrite. Both stay None on every
    # off/shadow turn and on every live turn where steering did not apply;
    # active_filters already carries whatever the deterministic path decided.
    route_live_scope: CompiledScope | None = None
    route_live_query: str | None = None
    # Audit-only bookkeeping for why live steering did or did not apply. Stays
    # "not_attempted" whenever resolve_product() resolves (or ambiguously
    # matches) a product directly: PR12 does not touch that path, so the
    # no-product branch is the only place this changes.
    route_live_outcome: str = "not_attempted"
    route_live_compiled_kind: str | None = None


def _route_history(turns: list[PriorTurn]) -> tuple[RouteHistoryTurn, ...]:
    """Converts persisted history to the bounded, application-labelled shape.

    Args:
        turns: Conversation memory, oldest first.

    Returns:
        The prompt-ready history. Bad historical metadata cannot make the route
        call fail or manufacture inheritance; it is downgraded to unscoped
        context.
    """
    history: list[RouteHistoryTurn] = []
    for turn in turns:
        scope_kind = turn.scope_kind if turn.scope_audited else "none"
        policy: CorpusPolicyHint | None = None
        if scope_kind == "corpus" and turn.corpus_policy:
            try:
                policy = CorpusPolicyHint(turn.corpus_policy)
            except ValueError:
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
    """Describes today's pre-model action in the route contract's vocabulary.

    Args:
        reason: The branch reason this turn took.
        response_mode: The status this turn is heading for.

    Returns:
        One of converse, lookup_clarify, or lookup.
    """
    if response_mode == "meta":
        return "converse"
    if response_mode in {"clarify", "scope_warning"} or reason in _ROUTE_CLARIFY_REASONS:
        return "lookup_clarify"
    return "lookup"


def _current_scope_for_shadow(state: TurnState, *, response_mode: str) -> str:
    """Describes the authoritative scope today's path actually reached.

    Args:
        state: The in-flight turn.
        response_mode: The status this turn is heading for.

    Returns:
        One of converse, product, or clarify.
    """
    if response_mode == "meta":
        return "converse"
    if state.active_filters.get("normalized_name"):
        return "product"
    return "clarify"


def _attach_route_shadow(state: TurnState, route_json: RouteJson) -> None:
    """Copies the finalized route observation onto an audit route, if any.

    Args:
        state: The in-flight turn.
        route_json: The audit route to mutate in place.
    """
    if state.route_shadow_audit is not None:
        route_json["route_call"] = dict(state.route_shadow_audit)


def _compile_route_live_scope(
    state: TurnState,
    *,
    question: str,
    session_id: str,
    session_filters: dict[str, Any],
) -> CompiledScope | None:
    """Compiles the live route decision into an executable scope (PR12).

    Only called from ``_resolve_and_carry_over``'s no-product branch, so
    ``resolved_product_filters`` is always None here: a question that directly
    names a product is still resolved deterministically by ``resolve_product``
    before this ever runs, unchanged by PR12.

    Args:
        state: The in-flight turn; its shadow observation supplies the decision,
            and ``route_live_outcome`` records why compilation did or did not
            produce a scope.
        question: The user's literal question.
        session_id: The shell's session id, for the prior audited scope.
        session_filters: The session's sticky product filters.

    Returns:
        The compiled scope, or None. Fails open like the rest of the route call:
        any exception here is a LIVE MODE failure, never a turn failure, so the
        caller falls back to exactly the heuristic path shadow/off already run.
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
# _session_filters/_recent_turns) as PARAMETER names on purpose: their bodies are
# transplanted verbatim from the pre-split ask_core, and identical names keep
# that move mechanically checkable against the pre-split code.


def _resolve_and_carry_over(
    state: TurnState,
    *,
    question: str,
    session_id: str,
    settings: Settings,
    _decline: Callable[..., tuple[RagOutcome, AuditPayload, SessionPatch]],
    _session_filters: Callable[[], dict[str, Any]],
) -> tuple[RagOutcome, AuditPayload, SessionPatch] | TurnState:
    """Resolves the product, then carries session form/route over.

    Args:
        state: The in-flight turn, mutated in place.
        question: The user's literal question.
        session_id: The shell's session id.
        settings: The live settings.
        _decline: The terminal-decline ceremony.
        _session_filters: Memoized reader for the session's sticky filters.

    Returns:
        The same TurnState to continue the turn, or a terminal decline triple
        from the resolution family.
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
            # returned "none", so no IN-corpus product is named; a fuzzy or brand
            # hit is then the remaining evidence that the user changed subject,
            # and inheriting the session's drug there would answer a question
            # about product A using product B's guidance.
            #
            # Residual, unchanged and bounded: a drug ABSENT from the corpus
            # (romidepsin scores ~60, under the 82 threshold) yields neither
            # candidate, so a follow-up naming it still carries over. Closing
            # that needs a drug-name detector the resolver does not have.
            suggestions = suggest_products(question)
            # One local Drugs@FDA lookup, two answers: the corpus generics behind
            # a brand name (the long-standing did-you-mean), and whether the name
            # is a real drug this corpus simply does not carry.
            external = lookup_external_drug(question)
            brand_matches = external.corpus_products
            session_filters = _session_filters()
            # PR12: ask the live route call whether this IS the genuine elliptical
            # follow-up the word-list heuristic keeps missing (issue #219). Only
            # in live mode, and only once suggestions/brand_matches are known
            # empty, so a route decision can never override the did-you-mean or
            # brand-lookup guards that catch a question naming a DIFFERENT
            # product -- the same anti-leak guard the heuristic branch relies on.
            route_scope: CompiledScope | None = None
            if settings.route_call_mode == "live":
                route_scope = _compile_route_live_scope(
                    state,
                    question=question,
                    session_id=session_id,
                    session_filters=session_filters,
                )
                if route_scope is not None:
                    if route_scope.kind is not CompiledScopeKind.PRODUCT:
                        # CORPUS/CLARIFY/CONVERSE: PR12 owns compiling and
                        # authorizing these (retrieve/scope.py, unchanged), not
                        # executing them. CORPUS in particular has no
                        # $in-membership translation in the retriever yet, and
                        # the post-retrieval mixed-products guard is not
                        # scope-aware yet (PR13's job), so wiring execution here
                        # would either bypass that guard's intent or break every
                        # corpus turn on it. A compiled corpus scope stays
                        # audited and dark, and the turn falls through to today's
                        # deterministic path exactly as shadow does.
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
                # Carry the product across turns. The chosen dosage_form/route are
                # carried just below, after resolved_name is set, so the same
                # logic also covers the single-product-corpus fallback path.
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
                # No product named. Offer a high-confidence "did you mean" for
                # genuine typos, then a brand-to-generic lookup (Adderall ->
                # amphetamine), else decline.
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
                    # product?" would be a lie: no answer would name one that
                    # works. A real evidence gap, so it keeps the refused
                    # register (INV-2). Sits BELOW the session carry-over and the
                    # did-you-mean/brand offers, which all have something
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
                # indistinguishable: both rendered as an "Evidence gap". They are
                # separate outcomes now, and neither is a refusal.
                unresolved = classify_unresolved(
                    question, external_drug_known=external.known_absent
                )
                if unresolved == "product_not_covered":
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
    # the none-branch carry-over above never runs. Dropping appl_no here would
    # widen a same-ingredient follow-up even if persistence retained the key;
    # dropping the form pair would re-trigger the multi-form clarify.
    if (
        resolved_name
        and not state.active_filters.get("dosage_form")
        and not state.active_filters.get("route")
        # PR12: a live-steered carry-over already trusts session_filters for
        # normalized_name above, so it must trust the same session's form/route
        # too. Otherwise a multi-form product the heuristic would have missed
        # carries a product but no form, and immediately hits the multi-form
        # clarify guard below instead of retrieving.
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
    settings: Settings,
    _decline: Callable[..., tuple[RagOutcome, AuditPayload, SessionPatch]],
    _session_filters: Callable[[], dict[str, Any]],
    _recent_turns: Callable[[], list[PriorTurn]],
) -> tuple[RagOutcome, AuditPayload, SessionPatch] | TurnState:
    """Runs every gate that can end the turn before retrieval.

    Order is load-bearing and unchanged: scope warning, meta (with its named-drug
    hard veto), the social gate, resolution and carry-over, the vague-input
    clarify, then the pre-retrieval multi-form guard.

    Args:
        state: The in-flight turn, mutated in place.
        question: The user's literal question.
        session_id: The shell's session id.
        settings: The live settings.
        _decline: The terminal-decline ceremony.
        _session_filters: Memoized reader for the session's sticky filters.
        _recent_turns: Memoized reader for conversation memory.

    Returns:
        The same TurnState to continue the turn, or a terminal decline triple.
    """
    if _is_scope_warning_request(question):
        return _decline(
            _scope_warning,
            reason="scope_warning",
            response_mode="scope_warning",
            # A caller-pinned product (API/dossier filter, already canonicalized
            # by ask_core) short-circuits resolution.
            filters=state.active_filters,
        )

    # Meta gate: "what does this system do" is answered from trusted system
    # state, then one bounded guidance turn, and no retrieval. This sits AFTER
    # the scope-warning check and BEFORE entity resolution/retrieval ON PURPOSE.
    # It is a HARD VETO: fire meta only when the phrase matches AND the question
    # does NOT resolve to a named in-corpus drug. A named-drug question that
    # happens to carry a meta phrase ("what BE study do you cover for
    # atorvastatin?") MUST skip meta and continue to the normal grounded path.
    # A caller-pinned product is likewise a resolved context, so it skips too.
    if (
        _is_meta_request(question)
        and not state.active_filters.get("normalized_name")
        and resolve_product(question).status != "resolved"
    ):
        return _decline(_meta, reason="meta", response_mode="meta")

    # Social gate: a pleasantry is a conversation, not a failed product lookup.
    # Sits here, BEFORE entity resolution, so a greeting costs no resolver work,
    # no external product lookup and no model call, and so it can never reach the
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
        settings=settings,
        _decline=_decline,
        _session_filters=_session_filters,
    )
    if not isinstance(resolved, TurnState):
        return resolved

    # The same read _resolve_and_carry_over ended on: normalized_name is settled
    # for the remainder of the turn once resolution has run.
    resolved_name = state.active_filters.get("normalized_name")

    # Bare drug name, or no real question: guide with options instead of dumping
    # a default BE answer. Fires however the product was pinned, whether named in
    # the question, set by an API/UI filter, or carried over, so a no-topic input
    # never reaches the synthesizer and comes back as a cited greeting. This
    # deterministic guard owns the options and status before the bounded guidance
    # planner sees the turn.
    #
    # A recognized drill-down ("tell me more", "why?") is EXEMPT once there is a
    # prior turn to anchor on: it is topic-less by construction, so the vague
    # gate would serve a clarify menu to a user who just asked to hear more about
    # the thing they were already discussing. _retrieval_query re-anchors the
    # embedding on that prior turn, which is what makes the exemption safe.
    #
    # The _recent_turns() conjunct is load-bearing, not belt-and-braces: with NO
    # history there is nothing to re-anchor on, the rewrite is the identity, and
    # exempting would trade today's useful clarify menu for a low_top_score
    # refusal. A bare drug name still clarifies; it matches no follow-up form.
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

    # Multi-form guard (pre-retrieval): the resolver pins only normalized_name,
    # but ~1 in 5 drugs span multiple dosage forms/routes (estradiol: transdermal
    # gel/spray vs. vaginal tablet/insert). Blending those into one LLM context
    # lets a wrong-form PSG be cited as if it answered the question, and the
    # blend is invisible because citation labels are appl-number-only. So once a
    # product is resolved, enumerate its CURRENT documents' distinct
    # (dosage_form, route) combos, honoring any form/route already pinned; if
    # more than one remains, CLARIFY which form before retrieving.
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
            # must NOT degrade to "no combos". Letting the DB error escape would
            # be an unaudited 500 the stream-fallback client re-runs into the
            # same down DB. Mirror the provider-error path instead: audited,
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
            # Before clarifying, honor a form the QUESTION already names: if
            # exactly one combo's dosage_form/route tokens uniquely match the
            # question text, pin it and proceed. A form-explicit question
            # shouldn't pay a clarify hop (and on the full catalog it would
            # otherwise flip answerable items to clarify).
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


def _refusal_threshold(settings: Settings) -> float:
    """Returns the cosine floor a passage must clear to enter synthesis (INV-2).

    Scoped to the ACTIVE EMBEDDING PROFILE, not global. A cosine threshold is a
    property of one model's score distribution: carrying 0.30 across the
    Qwen3-Embedding cutover would silently move the refusal rate without anyone
    deciding it should, in both directions and invisibly, since a refusal and a
    correct decline look identical in the audit row.

    Args:
        settings: The live settings; supplies the per-profile map and the legacy
            global used as the fallback.

    Returns:
        The threshold for the active profile, or the global value when that
        profile has no calibrated entry yet.
    """
    profile = (settings.active_embedding_profile or "legacy").strip()
    return settings.refusal_score_threshold_by_profile.get(
        profile, settings.refusal_score_threshold
    )


def _trim_evidence(
    passages: list[RetrievedPassage], settings: Settings, threshold: float
) -> list[RetrievedPassage]:
    """Trims the reranked wide net to the final evidence set.

    Args:
        passages: The wide net, already through the optional reranker.
        settings: Supplies the final k and the diversity flag.
        threshold: The INV-2 cosine floor, resolved once by the caller.

    Returns:
        The passages synthesis may cite. With the diversity flag off this is the
        plain first-``effective_rerank_top_k`` slice, unchanged.
    """
    if settings.mmr_diversity_enabled:
        # MMR may only spend slots on passages that can survive the INV-2
        # threshold applied just below; a "diverse" sub-threshold pick would be
        # dropped there, shrinking the evidence set below k and turning the
        # diversity A/B into a count confound. When nothing clears the threshold,
        # fall through to the plain slice so the refusal path (and its
        # related-passages surface) is identical to flag-off.
        eligible = [p for p in passages if p.score >= threshold]
        if eligible:
            return mmr_select(eligible, settings.effective_rerank_top_k)
    return passages[: settings.effective_rerank_top_k]


def _retrieve_and_group(
    state: TurnState,
    *,
    question: str,
    k: int | None,
    settings: Settings,
    _decline: Callable[..., tuple[RagOutcome, AuditPayload, SessionPatch]],
    _emit: Callable[[str], None],
    _recent_turns: Callable[[], list[PriorTurn]],
) -> tuple[RagOutcome, AuditPayload, SessionPatch] | TurnState:
    """Retrieves, reranks, thresholds, and runs the post-retrieval tripwires.

    Args:
        state: The in-flight turn, mutated in place.
        question: The user's literal question.
        k: Stage-1 retrieval width; None uses VECTOR_TOP_K.
        settings: The live settings.
        _decline: The terminal-decline ceremony.
        _emit: The cosmetic progress sink.
        _recent_turns: Memoized reader for conversation memory.

    Returns:
        The same TurnState, now carrying ``passages`` (the audit trail),
        ``evidence_passages`` (the only passages synthesis may see) and
        ``route_json``, or a terminal decline triple.
    """
    # The retrieval boundary runs on a typed scope rather than the raw dict. It
    # is a lossless view of state.active_filters (empty values dropped, exactly
    # as _build_where would), so the WHERE clause and the mode decision are
    # unchanged; see tests/test_product_scope.py. The dict itself still owns the
    # persistence boundaries below; only what retrieval reads moves here.
    scope = ProductScope.from_filters(state.active_filters)
    resolved_name = scope.normalized_name
    threshold = _refusal_threshold(settings)

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
    # contentless drill-down has no topical signal of its own. Identity for every
    # question that carries its own topic, so single-turn behaviour and the
    # offline eval are byte-identical. Orthogonal to the mode above: this decides
    # WHAT text is searched, the mode decides HOW the search runs.
    if state.route_live_query is not None:
        # PR12: the route call's standalone rewrite steered this turn instead of
        # the heuristic (_resolve_and_carry_over set both together, only in live
        # mode). It already retains every named study, metric, subpart and
        # qualifier by contract (route.py's ROUTE_SYSTEM), and it is what let
        # this turn reach retrieval at all.
        search_query = state.route_live_query
        route_json["retrieval_query_source"] = "route_live"
    else:
        search_query = _retrieval_query(
            question, normalized_name=resolved_name, prior_turns=_recent_turns()
        )
    # Persist WHETHER the rewrite fired, never the rewritten text: the audit row
    # must show that this turn searched on something other than the user's words,
    # and M7 (follow-up miss rate) counts exactly this flag.
    if search_query != question:
        route_json["retrieval_query_rewritten"] = True

    state.retrieval_block.update(
        RetrievalPlan(
            mode=retrieval_mode,
            scope=retrieval_scope,
            profile_id=(settings.active_embedding_profile or "legacy").strip(),
            dimension=0,
            k=k if k is not None else settings.vector_top_k,
        ).as_route_json()
    )
    with stage("retrieve"):
        passages = retrieve(search_query, k=k, filters=scope.as_filters(), mode=retrieval_mode)
    state.retrieval_block["returned"] = len(passages)
    # The threshold rides in the ledger so a profile cutover that moves the
    # refusal rate is attributable after the fact, not just observable.
    state.retrieval_block["threshold"] = threshold
    route_json["retrieval"] = cast(RetrievalBlock, dict(state.retrieval_block))

    # Stage 2: optional rerank, then trim. Same rewritten query: the reranker
    # scores relevance against the search intent, not the literal keystrokes.
    passages = rerank_passages(search_query, passages)
    # Near-clones of one section are one piece of evidence dressed as k (DSA S33).
    passages = _trim_evidence(passages, settings, threshold)

    # INV-2: weak passages never enter grounded synthesis. The constrained
    # guidance model still sees the QUESTION and trusted route/options, but not
    # the sub-threshold passage text, so it can choose a useful next step without
    # letting irrelevant evidence become citation cover. Gate on the MAX cosine
    # score, not passages[0]: the reranker (when enabled) reorders by a
    # cross-encoder score on a different scale, so passages[0].score may be a
    # demoted-but-still-present passage's cosine value. The threshold sits on the
    # cosine scale, so compare the true best cosine.
    if not passages or max(p.score for p in passages) < threshold:
        return _decline(
            _refuse,
            reason="low_top_score",
            response_mode="refused",
            passages=passages,
            # Surface the sub-threshold matches as inert "related" pointers
            # (distinct product NAMES only). refused/citations stay untouched, so
            # this never dresses the refusal as an answer.
            related=_related_from_passages(passages),
        )

    # One strong hit must not launder weaker neighbors into the prompt or the
    # citation allowlist. Keep all retrieved rows in the audit trail, but only
    # individually above-threshold passages may support synthesis.
    evidence_passages = [passage for passage in passages if passage.score >= threshold]

    # Post-retrieval guard (defense in depth): every passage must be the same
    # product. The filter guarantees this; this catches a caller that bypassed
    # the resolver. Mixed products means CLARIFY which (offer the distinct
    # products) rather than cite across them or bluntly refuse: the evidence is
    # unclear, so ask. Zero citations either way.
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

    # Same defense in depth for dosage form: even within one product, passages
    # must not blend distinct (dosage_form, route) combos (the wrong-form
    # citation bug). The pre-retrieval guard normally catches this; this
    # backstops a caller that bypassed it. Skipped when a passage is missing
    # form/route metadata, since a half-known combo would split docs that are
    # answerable together (same-combo beclomethasone docs).
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
    settings: Settings,
    _decline: Callable[..., tuple[RagOutcome, AuditPayload, SessionPatch]],
    _emit: Callable[[str], None],
    _recent_turns: Callable[[], list[PriorTurn]],
    _emit_draft: Callable[[str], None] | None = None,
    _emit_draft_reset: Callable[[], None] | None = None,
) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
    """Builds the prompt, runs one synthesis call, and dispatches on the gate.

    Args:
        state: The in-flight turn, carrying this turn's passages and route.
        question: The user's literal question.
        session_id: The shell's session id.
        turn_id: The shell's turn id.
        user_id: Audit attribution only.
        settings: The live settings.
        _decline: The terminal-decline ceremony.
        _emit: The cosmetic progress sink.
        _recent_turns: Memoized reader for conversation memory.
        _emit_draft: Sink for provisional, UN-GATED prose deltas.
        _emit_draft_reset: Retroactive discard signal for that draft channel.

    Returns:
        The (outcome, audit, patch) triple. Always terminal: every branch
        returns.

    Raises:
        D1ResidencyError: Propagated from the provider so the outer audited
            boundary can convert it to status="error".
    """
    passages = state.passages
    evidence_passages = state.evidence_passages
    route_json = state.route_json
    resolved_name = state.active_filters.get("normalized_name")

    _emit(f"Reading {len(evidence_passages)} matching guidance passage(s)…")

    # Conversational memory: thread the last few ANSWERED turns so a follow-up
    # ("what about the fed study?") resolves naturally. Context ONLY: citations
    # are stripped (_format_recent) and the system prompt forbids treating it as
    # a source. INV-1 still holds because the gate accepts only THIS turn's
    # passages, so a fact that lived only in a prior turn cannot acquire a valid
    # citation here and is dropped or refused. The current turn's just-written
    # user row is excluded by turn_id (the shell bakes that into the loader).
    # With no usable history the block is "" so the prompt is byte-identical to
    # the single-turn form, which protects the eval.
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
    # flag is a secret flip with no deploy, and the audit row must stamp the
    # identity of the path that actually produced it.
    prose_mode = settings.prose_synthesis_enabled
    selective_requested = settings.selective_citation_enabled
    # v7 selective citation is a POLICY change on top of v6's FORMAT change, so
    # it is honored only when prose is also on. A misconfigured
    # selective-without-prose is inert (the turn runs the v5 path exactly) but
    # observable (risk 6).
    selective_mode = prose_mode and selective_requested
    if selective_requested and not prose_mode:
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
        # v7 sends NO schema message either, like v6 (see the v6 comment below
        # for why). Own system prompt and own exemplar set (B.10.2), same
        # user/assistant message shape.
        synth_messages = [LLMMessage(role="system", content=GROUNDED_QA_SYSTEM_V7)]
        synth_messages.extend(
            LLMMessage(role=exemplar_role, content=exemplar_text)
            for exemplar_role, exemplar_text in GROUNDED_QA_EXEMPLARS_V7
        )
        synth_messages.append(LLMMessage(role="user", content=user_prompt))
    elif prose_mode:
        # v6 sends NO schema message anywhere -- the schema is the server-side
        # parser now -- and the few-shot exemplars ride between the system prompt
        # and the live turn as real user/assistant pairs. The tail restatement
        # inside GROUNDED_QA_USER_V6 is the true last-read text on the Databricks
        # path, where all system content is front-loaded on that wire.
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
            # thing the model reads and never has to survive a .format() pass.
            TURN_SCHEMA_MESSAGE,
        ]

    # What the synthesis call actually cost and whether it had to retry: two
    # facts that lived only in a structlog line (or, for the retry, nowhere
    # durable at all), which is why "is our malformed_structure rate a token cap
    # hit or a JSON syntax error?" is unanswerable from the DB today.
    # synth_route holds the SAME dict object, not a copy: the completion helpers
    # fill it in during the call, and every branch below reads it afterwards.
    synth_telemetry: dict[str, Any] = {"max_output_tokens": settings.synthesizer_max_tokens}
    synth_route: RouteJson = {"synthesis": synth_telemetry}

    def _run_synthesis(messages: list[LLMMessage]) -> LLMResponse:
        """Issues one synthesis completion over ``messages``.

        Extracted so the bounds repair below can issue its second attempt through
        the SAME branch logic (streamed vs buffered, prose vs JSON) rather than a
        copy that could drift from it.

        Args:
            messages: The full prompt for this attempt.

        Returns:
            The terminal LLMResponse.
        """
        if prose_mode and _emit_draft is not None:
            return _stream_structured(
                provider,
                messages,
                max_tokens=settings.synthesizer_max_tokens,
                telemetry=synth_telemetry,
                on_delta=_emit_draft,
                # A missing reset sink degrades to a no-op, not a hold: the caller
                # opted into drafts but not the retroactive-discard signal (a
                # direct ask() call in tests), and _stream_structured already
                # treats a failing sink as cosmetic.
                on_reset=_emit_draft_reset or (lambda: None),
            )
        return _complete_structured(
            provider,
            messages,
            max_tokens=settings.synthesizer_max_tokens,
            response_format=None if prose_mode else "json",
            telemetry=synth_telemetry,
        )

    try:
        with stage("synthesis"):
            response = _run_synthesis(synth_messages)
    except Exception as exc:  # Provider transport error (timeout / 429 / 5xx).
        # B2: a synthesizer failure must NOT return a naked 500 with no audit
        # row, which would break INV-6 exactly when the system misbehaves.
        # Degrade to a graceful, audited refusal and surface the cause to Sentry.
        # The error never reaches the user verbatim.
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
    # max_tokens truncation or a provider hiccup) is not an answer. Refuse rather
    # than fall through and emit a non-refused, zero-citation empty "answer".
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
    # loop, a sentence that never terminates -- would otherwise render unbounded
    # text. ONE repair attempt, then a conversational exit.
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
                with _best_effort("on_draft_reset_failed"):
                    _emit_draft_reset()
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
        # route_json top-level key set is a contract, and the parse record is
        # synthesis forensics (INV-6) -- what the parser killed and why.
        synth_telemetry["prose_parse"] = {
            "claims_parsed": len(parsed_prose.claims),
            "killed": [t[:400] for t in parsed_prose.leftover_brackets],
            "truncated_material": parsed_prose.truncated_material,
        }
        if selective_mode:
            # CONDITIONAL: emitted only under v7, so v6's ledger bytes (and
            # tests/test_prose_synthesis.py's exact-dict pin) never move.
            synth_telemetry["prose_parse"]["kinds"] = [c.kind for c in parsed_prose.claims]

        killed_material: str | None = None
        for killed in parsed_prose.leftover_brackets:
            killed_material = tg.materiality_trigger(killed)
            if killed_material is not None:
                break
        if parsed_prose.truncated_material or killed_material is not None:
            # The parser removed materially-worded text (an unterminated tail or
            # a bracket-killed sentence). What survives could read as its own
            # opposite, so the whole answer is rejected: the same OD-4 stance as
            # the gate's material-drop verdict, one layer earlier.
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
            # Reason string kept as malformed_structure so the ops greps and the
            # ~12% prod baseline stay comparable; in prose mode it means "no
            # sentences parsed", not "schema violation".
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
        # corpus. Serving the configured refusal text here ("I couldn't find this
        # in the current FDA guidance corpus") would record a claim about
        # coverage that was never tested, in the audit row, forever.
        log.warning("qa_malformed_structure", detail=admitted.detail[:500])
        gate_route: RouteJson = {
            "synthesis": synth_telemetry,
            "gate_failure": {
                "class": _gate_failure_class(admitted.detail),
                "detail": admitted.detail[:200],
            },
        }
        return _decline(
            _refuse,
            reason=admitted.reason,
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            status="error",
            answer_text=_SERVICE_UNAVAILABLE_TEXT,
            usage=response.usage,
            route_extra=gate_route,
            guide=False,
        )

    if (
        parsed_prose is not None
        and parsed_prose.leftover_brackets
        and admitted.verdict == tg.VERDICT_ANSWER
    ):
        # OD-5 continuity for parser kills: a benign bracket-killed sentence
        # never reached the gate, so the verdict alone would render a clean
        # fully-cited answer while text was silently removed. Folding to PARTIAL
        # makes render_answer disclose the omission in the same plain language as
        # a gate drop; the removed sentences are in the prose_parse telemetry.
        admitted = replace(admitted, verdict=tg.VERDICT_PARTIAL)

    log.info("qa_turn_gate", **_gate_log_fields(admitted))

    # v7 found-nothing: the model's own decline text, re-scanned against the
    # materiality and source-assertion lexicons before it can be served
    # (B.10.1.2). Computed here (only for the one verdict render_decline is
    # documented to accept) so the ledger below can carry the guard on every
    # branch.
    decline_text: str | None = None
    decline_guard: str | None = None
    if admitted.verdict == tg.VERDICT_CONVERSATIONAL_DECLINE:
        decline_text, decline_guard = tg.render_decline(admitted)
        if decline_guard is not None:
            # The parser and the gate disagreed (a caller passing kinds that do
            # not match the texts) and defense in depth fired. Never serves the
            # guarded text; the caller below falls back to canned copy.
            log.warning("qa_decline_guard_fallback", guard=decline_guard)

    # OD-5's operator half rides on EVERY post-gate audit row, not just the
    # answer path. The decline branches below are exactly the turns where a claim
    # was DROPPED (no_valid_citations, material_drop), so persisting the ledger
    # only on the answer path would leave the per-claim drop reason and the
    # offending (short_name, page) pairs in a structlog line and nowhere in the
    # DB, the opposite of what turn_gate.ledger claims to provide. Built once
    # here so the answer and decline paths cannot drift.
    turn_route: RouteJson = {
        "synthesis": synth_telemetry,
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
        # vague inputs that _looks_vague didn't catch), so guide. When the product
        # came from the single-product fallback (no drug named), a decline is a
        # genuine "not covered", so stay refused (INV-2).
        if state.resolved_by_name and resolved_name:
            return _decline(
                _clarify,
                reason="model_refusal",
                response_mode="clarify",
                # Post-retrieval decline: keep the passages that the model saw
                # and judged insufficient. These are the only scored negatives
                # the system produces -- a real product resolved, retrieval ran,
                # and the evidence still did not support an answer -- so without
                # them the audit row cannot say WHAT was weak, and the refusal
                # threshold has no observations to be calibrated against.
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
        # OD-4: what was dropped carried obligation/permission/exception wording,
        # so the surviving claims can read as their own opposite. Reject the whole
        # answer rather than hand back a confident, fully cited,
        # faithfulness-1.0 statement with the qualifier deleted.
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
        # v7 found-nothing: the model says so in its own words. Wire shape is the
        # v5/v6 refusal contract exactly (refused, citations=[], reason
        # model_refusal); only the TEXT is model-authored, and only after
        # render_decline re-scanned every sentence it is about to serve. Never
        # forks to _clarify (AMENDMENT 1, B.10.1.3): that REPLACES the answer text
        # with application copy plus options, discarding the model's prose, which
        # is the entire feature.
        return _decline(
            _refuse,
            reason="model_refusal",
            response_mode="refused",
            passages=passages,
            model_name=response.model,
            # None (a guard fired) makes _refuse serve the canned copy, never the
            # guarded text.
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
        route_json=dict(route_json),
        # No-audit-no-answer (INV-6): a validated answer with no audit row is
        # never returned, but the failure must be DEFINED, not a naked 500 that
        # the stream-fallback client re-runs (a second paid synthesis) into the
        # same down DB. allow_skip=False makes the shell use the STRICT write; on
        # failure it serves this fallback, whose own audit is re-attempted and
        # skipped (flagged) if the DB is still down. Built eagerly because it is
        # pure: active_filters/context_applied no longer mutate after synthesis,
        # so build-time and failure-time route_json are identical.
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
            # the turn, so it carries the ledger too. Otherwise a PARTIAL
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
    """Computes one grounded Q&A turn without performing any writes.

    The PURE half of a turn: load context, compute, describe. Every branch
    (answer, refusal, clarify, meta, error) returns a triple and the ``ask()``
    shell -- later, the Go control plane -- performs the writes. Reads are
    allowed: retrieval reads the vector store, the resolver reads products, and
    session context arrives through the two shell-owned loaders, invoked lazily
    at exactly the pre-split call points so turns that never carry context over
    still skip the reads.

    Args:
        question: The user's literal question.
        session_id: The shell's already-ensured session id; threaded, not minted.
        turn_id: The shell's already-minted turn id.
        filters: Caller-pinned retrieval filters (API or dossier). A
            ``normalized_name`` here is canonicalized before use.
        k: Stage-1 retrieval width; None uses VECTOR_TOP_K.
        user_id: Audit attribution only.
        load_session_filters: Shell-owned reader for the session's sticky filters.
        load_recent_turns: Shell-owned reader for conversation memory.
        on_progress: Cosmetic phase strings, best-effort, never answer-bearing.
            There is deliberately NO token sink here: answer text is replayed by
            the shell after the audit write.
        on_draft: The ONLY un-gated bytes the core may emit, riding the
            dual-gated provisional draft channel (owner-amended INV-1,
            2026-08-10). Never presented as validated.
        on_draft_reset: Retroactive discard signal for that draft channel.

    Returns:
        The (RagOutcome, AuditPayload, SessionPatch) triple describing the turn
        and everything the shell must persist for it.

    Raises:
        D1ResidencyError: A residency violation fails the turn loudly rather than
            degrading into a parse failure or a retry against the fenced
            endpoint.
    """
    settings = get_settings()

    def _emit(textline: str) -> None:
        """Forwards one cosmetic progress phase, swallowing sink failures.

        Args:
            textline: The phase string.
        """
        if on_progress is None:
            return
        with _best_effort("on_progress_failed"):
            on_progress(textline)

    model_name = current_model_name(role="synthesizer")
    active_filters: dict[str, Any] = dict(filters or {})
    # Product-key hardening: a caller (API / dossier / clarify option) may pass a
    # normalized_name in any casing or salt-order. Canonicalize it to the exact
    # key the corpus stores so retrieval's exact-match filter cannot silently
    # miss and turn a real product into a wrong refusal.
    if active_filters.get("normalized_name"):
        active_filters["normalized_name"] = canonical_name(str(active_filters["normalized_name"]))

    # Up to two carry-over sites below read the session filters (product in the
    # resolver's none-branch, then dosage_form/route after resolution). Nothing
    # mutates them mid-turn (the shell applies filter updates after the turn), so
    # fetch lazily ONCE and reuse instead of two identical row reads per
    # follow-up. Lazy so turns that never carry over still skip the read.
    session_filters_memo: dict[str, Any] | None = None

    def _session_filters() -> dict[str, Any]:
        """Reads the session's sticky filters at most once per turn.

        Returns:
            The session filters, possibly empty.
        """
        nonlocal session_filters_memo
        if session_filters_memo is None:
            session_filters_memo = load_session_filters()
        return session_filters_memo

    recent_turns_memo: list[PriorTurn] | None = None

    def _recent_turns() -> list[PriorTurn]:
        """Reads conversation history at most once per turn.

        Two callers need it -- the retrieval rewrite before the vector search,
        and the synthesizer's conversation block after it -- and
        ``load_recent_turns`` is a DB read. Memoized so widening the follow-up
        path does not double the per-turn query count.

        Returns:
            Conversation memory, oldest first, possibly empty.
        """
        nonlocal recent_turns_memo
        if recent_turns_memo is None:
            recent_turns_memo = load_recent_turns()
        return recent_turns_memo

    response_mode: QueryStatusLiteral = "summary" if _is_summary_request(question) else "answer"
    state = TurnState(active_filters=active_filters, response_mode=response_mode)
    # Shadow context uses independent reads. It must never populate the
    # authoritative memo above before today's deterministic pipeline reaches its
    # normal read point: doing so would shift the snapshot used by a
    # concurrent-session follow-up and could change real behavior.
    route_shadow_session_filters: dict[str, Any] = {}

    def _finalize_route_shadow(*, reason: str, response_mode: str) -> None:
        """Compiles and compares the route observation once, changing nothing.

        Args:
            reason: The branch reason this turn took.
            response_mode: The status this turn is heading for.
        """
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
        if settings.route_call_mode == "live":
            # PR12: what actually steered this turn, distinct from
            # finalize_route_observation's agrees_with_mode/scope above, which
            # compare the route's PROPOSAL to today's path regardless of whether
            # live mode used it. "applied" means the carry-over ran off
            # route_live_scope's compiled product filters and standalone rewrite,
            # not the word-list heuristic; every other outcome describes why it
            # did not, including a live route call that itself failed.
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
        route_extra: RouteJson | None = None,
        guide: bool = True,
        **kw: Any,
    ) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
        """Runs the one ceremony every terminal decline branch shares.

        Builds the audit route and the result TOGETHER so the reason and
        response_mode pairing is single-source: a branch can no longer record an
        audit route that silently disagrees with the turn it describes. Reads
        ``state.active_filters`` and ``state.context_applied`` at CALL time,
        since they mutate as the pipeline advances.

        Args:
            maker: One of _refuse / _clarify / _scope_warning / _meta / _converse.
            reason: The machine-readable branch reason.
            response_mode: The status this branch is heading for.
            route_extra: Extra audit route keys (synthesis, turn, gate_failure).
                A NAMED keyword, so it is never forwarded to ``maker``.
            guide: Whether this branch may spend one constrained router
                completion. Healthy PRE-synthesis branches keep True;
                post-synthesis and error branches set it False because an AI call
                already happened or the failure makes another inappropriate. Also
                a NAMED keyword.
            **kw: Forwarded to ``maker``. Post-synthesis branches override
                model_name (response.model) and pass usage here.

        Returns:
            The (outcome, audit, patch) triple for this decline.

        Raises:
            D1ResidencyError: From the guidance call, so the outer audited
                boundary converts it into status="error".
        """
        route_json = _route_json(
            filters=state.active_filters,
            reason=reason,
            context_applied=state.context_applied,
            response_mode=response_mode,
        )
        # Declines that happen AFTER stage-1 search still describe a retrieval
        # that ran, so the plan belongs on their audit row too. A FRESH route is
        # built here, so it cannot inherit the answer path's mutation.
        if state.retrieval_block:
            route_json["retrieval"] = cast(RetrievalBlock, dict(state.retrieval_block))
        if route_extra:
            route_json.update(route_extra)
        _finalize_route_shadow(reason=reason, response_mode=response_mode)
        _attach_route_shadow(state, route_json)
        kw.setdefault("model_name", model_name)
        outcome, audit = maker(
            question=question,
            reason=reason,
            session_id=session_id,
            turn_id=turn_id,
            user_id=user_id,
            route_json=route_json,
            **kw,
        )

        # The planner earns its round trip only when it can change something. On
        # these two reasons the rendered copy is fixed at the call site, so with
        # no options to order the call is pure cost, which is what every "Hello"
        # used to pay. Deliberately NARROW: low_top_score, model_refusal,
        # no_valid_citations and material_drop pick between two sentences via
        # plan.next_step, so they keep calling even with no options.
        if reason in {"need_product", "product_not_covered"} and not (
            outcome.clarify or outcome.related
        ):
            guide = False

        if guide and outcome.status != "error":
            # Every healthy Ask turn reaches exactly one AI path. On a branch that
            # cannot safely synthesize a cited answer, the router model selects a
            # server-allowlisted NEXT STEP and existing option IDs. It never
            # writes display prose, changes status, invents filters, or sees weak
            # passage text. A provider or shape failure keeps the trusted
            # deterministic reply.
            product = str(state.active_filters.get("normalized_name") or "").strip() or None
            # An ambiguous or suggested candidate is not trusted product context.
            # Scope guidance is the one handler that may resolve a product inside
            # its deterministic maker without updating active_filters; recover it
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
            route_json["prompt"] = QUERY_GUIDANCE_PROMPT.as_dict()
            route_json["guidance"] = {"attempted": True, "applied": False}
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
                log.warning("qa_guidance_provider_error", error_type=type(exc).__name__)
                capture_exception(exc)
                route_json["guidance"]["fallback_reason"] = "provider_error"
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
                    route_json["guidance"]["fallback_reason"] = str(exc)
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
                    route_json["guidance"] = {
                        "attempted": True,
                        "applied": True,
                        "next_step": plan.next_step,
                        "option_ids": list(plan.option_ids),
                        "selected_options": selected_option_records(plan, request),
                    }
        # The audit was built by `maker(...)` ABOVE with `route_json=dict(...)`,
        # a shallow copy taken BEFORE the guidance attempt ran. Everything the
        # guidance block just recorded -- route_json["guidance"]["attempted"],
        # ["applied"], ["fallback_reason"] -- therefore lands on the local dict
        # and never reaches the persisted row, so a router-attributed provider
        # failure audits as though no router was ever called. Re-sync the final
        # route onto the audit before it is written.
        audit.route_json = dict(route_json)
        return (
            outcome,
            audit,
            _build_patch(outcome, filters=state.active_filters, route_json=route_json),
        )

    if settings.route_call_mode != "off":
        if settings.route_call_mode == "live":
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
            configured_mode=settings.route_call_mode,
            question=question,
            recent_turns=_route_history(route_shadow_recent_turns),
            trusted_product_context=trusted_product,
            max_tokens=settings.route_call_max_tokens,
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
        settings=settings,
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
        settings=settings,
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
        settings=settings,
        _decline=_decline,
        _emit=_emit,
        _recent_turns=_recent_turns,
        _emit_draft=on_draft,
        _emit_draft_reset=on_draft_reset,
    )


def _pipeline_error(
    question: str,
    *,
    session_id: str,
    turn_id: str,
    user_id: str | None,
    filters: dict[str, Any] | None,
) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
    """Describes the DEFINED result of an unexpected pipeline crash.

    A fixed-copy status="error" refusal turn, skip-audited (``allow_skip`` stays
    the AuditPayload default). Closes the INV-6 gap ``ask()`` left open: a raise
    in retrieve/rerank/resolve now yields a row to write, never a naked 500.
    Reuses ``_refuse`` and ``_build_patch`` so the audit, result and patch stay
    single-source.

    Args:
        question: The user's literal question.
        session_id: The shell's session id.
        turn_id: The shell's turn id.
        user_id: Audit attribution only.
        filters: The caller-pinned filters, recorded on the audit route.

    Returns:
        The (outcome, audit, patch) triple for the crash.
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
    return (
        outcome,
        audit,
        _build_patch(outcome, filters=active_filters, route_json=route_json),
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
    """Answers a grounded Q&A turn with citations, a clarify, or a decline.

    The thin persistence SHELL around ``ask_core``: it owns the session ids and
    every write (user message, audit row, assistant message, filter carry-over)
    while the core owns every decision. A future control plane replaces this
    function without touching the core.

    Args:
        question: The user's literal question.
        filters: Caller-pinned retrieval filters.
        k: Stage-1 retrieval width; None uses VECTOR_TOP_K.
        session_id: An existing session to continue, or None to create one.
        user_id: The owner, or audit-only attribution when bind_session is False.
        turn_id: A caller-minted turn id, or None to mint one.
        bind_session: False keeps ``user_id`` as audit-only attribution (INV-6):
            the bookkeeping ChatSession stays unowned (user_id NULL) and so
            invisible to /sessions. That is for internal callers like the
            dossier, whose synthetic Q&A must not appear in the caller's chat
            history.
        origin: Forwarded to ``ensure_session``, landing ONLY when the session row
            is newly CREATED this call; an existing session's origin never
            changes underneath it (issue #208). "assistant" means the
            conversation is kept (readable, deletable) but stays out of the work
            rail's Threads list: the Research Studio panel's own scratch
            conversation, as opposed to the analyst's real work.
        on_progress: Short, cosmetic phase strings for a live status ticker (POST
            /query/stream). Carries NO answer text or citations, and a failing
            sink can never break or slow the query.
        on_token: The FINAL answer text in chunks, for a live typing effect. It
            fires only after the audit row is committed and only on an
            answer/summary turn, so every byte it emits is gated, rendered and
            audited: a declined or retracted draft can never reach it.
        on_draft: LIVE, un-gated, provisional prose deltas during synthesis,
            prose mode only, under the owner-amended INV-1 (2026-08-10). Never
            validated, never replayed on declines the way on_token is; the
            terminal result stays authoritative.
        on_draft_reset: Retroactive discard signal for that draft channel.

    Returns:
        The public QAResult for the turn.

    Raises:
        SessionOwnershipError: An ownership race was lost after the API's
            pre-check. Aborts rather than writing this caller's turns into
            another user's session; the API maps this to its ownership 404.
        SessionOriginError: The origin was outside SESSION_ORIGINS, a programming
            error in the caller rather than the transient DB hiccup the generic
            degrade exists for.
    """
    # Touch settings and the model name BEFORE any write, matching the pre-split
    # ask(): both are lru_cache-backed (near-free after the first call) but a
    # first-ever misconfiguration (a Settings validation error) must fail BEFORE
    # the user-message write below, not leave an orphaned question with no
    # assistant or audit response. ask_core re-reads them from cache.
    get_settings()
    current_model_name(role="synthesizer")

    # Turn clock. Stamped before the user-message write, matching Go's t0
    # (query.go, before persistUserTurn) so relay-path and native-path latency_ms
    # measure the same interval and are comparable in one percentile.
    t0 = perf_counter()

    # Stage timing spans the session write and compute but NOT _persist_turn:
    # the audit row cannot contain the duration of its own write. The gap
    # between measured_total_ms and the row's latency_ms is the unattributed
    # remainder (persistence included), which is the number this exists to
    # expose.
    with collect_stage_timings() as timings:
        # Session bookkeeping is best-effort: a DB hiccup here must never stop the query
        # from being processed and audited (INV-6). Degrade to a fresh id on failure.
        # The user-message write stays HERE, before compute, so a core exception still
        # leaves the question in the chat history exactly as before the split.
        try:
            with stage("session_write"):
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
    _attach_stage_timings(audit, timings)
    return _persist_turn(outcome, audit, patch, t0, on_token)


def compute_turn(
    question: str,
    *,
    filters: dict[str, Any] | None = None,
    k: int | None = None,
    session_id: str,
    turn_id: str,
    user_id: str | None = None,
) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
    """Computes one turn for the step-5 Go control plane, writing nothing.

    Runs ``ask_core`` behind the shell-owned session-context loaders (the same
    call ``ask()`` makes) but performs NO writes and adds the AUDITED-ERROR
    BOUNDARY: any unexpected raise in the pipeline (the retrieve/resolver gap
    ``ask()`` left unaudited) is caught here and turned into a DEFINED
    status="error"/pipeline_error outcome, so the caller (the Go CompleteQuery
    handler over POST /internal/query/compute) always receives a row to persist.

    Buffered path only; streaming stays in ``ask()`` (R3), so there are no
    on_progress or on_token sinks.

    Args:
        question: The user's literal question.
        filters: Caller-pinned retrieval filters.
        k: Stage-1 retrieval width; None uses VECTOR_TOP_K.
        session_id: The caller's already-minted session id.
        turn_id: The caller's already-minted turn id.
        user_id: Audit attribution only.

    Returns:
        The (outcome, audit, patch) triple for the caller to persist.
    """
    get_settings()
    current_model_name(role="synthesizer")
    # The native path is where production traffic actually flows, so it gets
    # the same stage timings as the relay path. Go owns the write, and it
    # persists this route_json verbatim, so the block reaches query_log
    # unchanged. There is no session_write stage here: Go performed that write
    # before calling, and its own timing belongs to Go.
    with collect_stage_timings() as timings:
        try:
            outcome, audit, patch = ask_core(
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
            outcome, audit, patch = _pipeline_error(
                question,
                session_id=session_id,
                turn_id=turn_id,
                user_id=user_id,
                filters=filters,
            )
    _attach_stage_timings(audit, timings)
    return outcome, audit, patch
