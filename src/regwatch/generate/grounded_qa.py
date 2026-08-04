"""Grounded Q&A orchestration.

Flow:
  1. Resolve product/form and retrieve top-k passages when the turn is answerable.
  2. Every healthy Ask turn reaches one AI path: constrained query guidance for
     a pre-synthesis non-answer outcome, or grounded synthesis for evidence.
  3. The synthesis path sends only above-threshold evidence with the grounding
     prompt and turn JSON Schema, in buffered json mode.
  4. Hand a synthesis completion to ``turn_gate.admit_turn``, which parses it and admits
     CLAIMS one at a time against the passages actually sent this turn.
  5. Dispatch on the gate's verdict: render the admitted claims, decline, or --
     when the payload did not parse -- serve the service-error copy.
  6. Write an audit log row (INV-6) regardless of outcome and return.

The synthesizer no longer writes prose or citation markers, so there is no
per-sentence prose gate any more: every user-visible byte on an answer turn is
either an admitted claim or renderer-authored (see generate/turn_gate.py).

Strangler Step 2: ``ask_core`` computes steps 1-5 and RETURNS what to persist
(rag_contract dataclasses); the ``ask()`` shell owns step 6 and every other
write. Callers see the unchanged ``ask()`` / ``QAResult`` surface.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Any

from config.settings import get_settings
from sqlalchemy import func
from sqlmodel import select

from regwatch.common.audit import log_query
from regwatch.common.citations import strip_all_citations, strip_sources_trailer
from regwatch.common.conversation import (
    PriorTurn,
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
    GROUNDED_QA_PROMPT,
    GROUNDED_QA_SYSTEM,
    GROUNDED_QA_USER,
)

# The core/shell contract types live in rag_contract (a pipeline-free module so
# they can become a cross-service HTTP contract). Citation / ClarifyOption /
# QueryStatusLiteral are spelled "as" themselves because api.main, the dossier
# stubs, and the tests import them from grounded_qa -- that public surface must
# not move, and mypy strict (no_implicit_reexport) only re-exports the aliased
# form.
from regwatch.generate.rag_contract import (
    AuditPayload,
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
from regwatch.generate.turn_gate import AdmittedTurn, GateFailure, admit_turn
from regwatch.generate.turn_schema import TURN_SCHEMA_MESSAGE
from regwatch.retrieve.reranker import rerank_passages
from regwatch.retrieve.resolver import resolve_brand, resolve_product, suggest_products
from regwatch.retrieve.retriever import RetrievedPassage, retrieve
from regwatch.store.db import session_scope
from regwatch.store.models import PsgDocument
from regwatch.store.queries import count_documents, current_dosage_form_routes
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
    # "retrieval") — surfaced so callers/eval can tell WHY we clarified or
    # refused, not just that we did. Mirrors route_json["reason"].
    reason: str | None = None
    interpretation: str | None = None
    clarify: list[ClarifyOption] = field(default_factory=list)
    # Sibling of `clarify` for the REFUSE family: when we decline (refused=true,
    # citations=[]), `related` surfaces inert "related, not an answer" pointers —
    # distinct product NAMES + their source link only, never passage text/score.
    # It NEVER changes the refusal contract (refused stays true, citations stay
    # []); it is purely additive context the UI renders as re-runnable pills.
    related: list[ClarifyOption] = field(default_factory=list)
    session_id: str | None = None
    turn_id: str | None = None


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
)
_FOLLOW_UP_PRONOUNS = frozenset({"it", "its", "this", "that", "same"})
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
# regulatory fact. False-negatives are SAFE — a missed meta phrase just falls
# through to the grounded cite-or-refuse path. False-positives are the danger
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
# Hard ceiling on a single synthesis call, INDEPENDENT of the setting. The
# truncation retry doubles the budget, so an operator who raises the setting
# must not be able to make one turn cost an unbounded number of tokens.
_SYNTH_MAX_TOKENS_CEILING = 4000


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

    Phrase-match ONLY — no LLM judges intent (an LLM mis-call would be the exact
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
        # committed by this point, so a failure here — e.g. the degraded
        # session_id=turn_id fallback has no chat_session row and the assistant
        # FK insert fails on Postgres — must not 500 an already-audited turn.
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


def _related_from_passages(passages: list[RetrievedPassage]) -> list[ClarifyOption]:
    """ "Related, not an answer" pointers from the sub-threshold passages in hand.

    Surfaces DISTINCT product NAMES ONLY — never the passage text or score
    (chunk text would read as quasi-evidence on a refusal). Deduped by product
    name, first occurrence wins (retrieval order = best match first). Each
    option re-runs as a name-scoped query, so it renders as an inert,
    re-runnable pill — never a citation chip. Refused/citations are untouched.
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
    so retrieval is constrained to a single form — the citation can no longer be
    to the wrong-form PSG. Filters round-trip verbatim through the API/UI.
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
        SILENTLY pins the wrong form — a wrong-form citation, the worst INV-1 outcome.
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
        return None  # the question named no form at all — clarify
    if len(scored) > 1 and scored[1][0] == best_score:
        return None  # two combos fit equally well (match AND completeness) — clarify
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


def _format_recent(turns: list[PriorTurn]) -> str:
    """Render prior turns as a compact, citation-free conversation context block.

    Citations are stripped so the model cannot see — and therefore cannot parrot
    — a stale ``[PSG, p.N]`` whose page may not be in THIS turn's passages, and
    each side is capped so the current passages stay dominant in the window.
    Reference-only, never evidence (INV-1): the system prompt forbids treating it
    as a source, and _validate_citations accepts only markers grounded in this
    turn's passages regardless.
    """
    lines: list[str] = []
    for t in turns:
        # Strip markers from BOTH sides: a citation-shaped token in a prior
        # question is context too, never a source the model may reuse this turn.
        q = strip_all_citations(t.question).strip()[:400]
        # Stored answers end with a "Sources:" trailer. Current entries are
        # bracketed, while legacy entries were not; drop the whole trailer before
        # the bracket strip (shared with eval/metrics.faithfulness) so neither
        # form can become a stale re-citable pointer in conversation memory.
        answer_prose = strip_sources_trailer(t.answer)
        a = strip_all_citations(answer_prose).strip()[:600]
        if not q and not a:
            continue
        lines.append(f"User: {q}\nAssistant: {a}")
    return "\n\n".join(lines)


def _complete_structured(
    provider: LLMProvider,
    messages: list[LLMMessage],
    *,
    max_tokens: int,
) -> LLMResponse:
    """One buffered json-mode completion, with a single 2x truncation retry.

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
    try:
        return provider.complete(
            messages,
            temperature=_SYNTH_TEMPERATURE,
            max_tokens=capped,
            response_format="json",
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
        if capped >= _SYNTH_MAX_TOKENS_CEILING:
            raise
        log.warning("qa_synthesis_truncation_retry", old=capped, error=str(exc)[:200])
        return provider.complete(
            messages,
            temperature=_SYNTH_TEMPERATURE,
            max_tokens=min(capped * 2, _SYNTH_MAX_TOKENS_CEILING),
            response_format="json",
        )


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
_SERVICE_UNAVAILABLE_TEXT = (
    "The answer service is temporarily unavailable. Your question was "
    "not answered — please try again in a moment."
)


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
    """Whole-millisecond turn wall time, or None — Go's ``latencyMs`` twin.

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
    time — the same split Go's ``auditParams`` makes.

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
        # failure must NOT break the refusal — fall back to the generic decline.
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
    """Assemble a meta answer from VERIFIED SYSTEM STATE ONLY — never an LLM.

    Three independent system facts, each read live and clearly labeled so the
    two are NEVER conflated:
      * corpus      — the askable PSGs (distinct normalized_name + doc count),
      * watchlist   — the products Watch actively monitors (list_watchlist),
      * what changed — the most recent durable alerts (latest_digest_records),
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

    # Watchlist: products Watch MONITORS — distinct from the askable corpus above.
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
            # text (see process/change_detector.summarize_change) — a regulatory
            # claim, NOT a system fact — so they must NEVER reach this uncited
            # meta answer (INV-1). Detail lives on the cited Watch feed.
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
    -- answer text is replayed by the shell after the audit write, so the core
    emits no user-visible bytes at all.
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

    resolved_by_name = False
    context_applied = False
    response_mode: QueryStatusLiteral = "summary" if _is_summary_request(question) else "answer"

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
        Reads active_filters/context_applied at CALL time (they mutate as the
        pipeline advances); post-synthesis branches override model_name
        (response.model) and pass usage via **kw.

        ``route_extra`` and ``guide`` are NAMED keywords, so neither is forwarded
        to ``maker`` via **kw. Healthy PRE-synthesis terminal branches keep
        ``guide=True`` and therefore attempt one constrained router completion;
        post-synthesis/error branches set it false because an AI call already
        happened or the failure makes another call inappropriate."""
        rj = _route_json(
            filters=active_filters,
            reason=reason,
            context_applied=context_applied,
            response_mode=response_mode,
        )
        if route_extra:
            rj.update(route_extra)
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

        if guide and outcome.status != "error":
            # Every healthy Ask turn reaches exactly one AI path. On a branch that
            # cannot safely synthesize a cited answer, the router model selects a
            # server-allowlisted NEXT STEP and existing option IDs. It never writes
            # display prose, changes status, invents filters, or sees weak passage
            # text. A provider/shape failure keeps the trusted deterministic reply.
            product = str(active_filters.get("normalized_name") or "").strip() or None
            # An ambiguous/suggested candidate is not trusted product context.
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
        return outcome, audit, _build_patch(outcome, filters=active_filters, route_json=rj)

    if _is_scope_warning_request(question):
        return _decline(
            _scope_warning,
            reason="scope_warning",
            response_mode="scope_warning",
            # A caller-pinned product (API/dossier filter, already
            # canonicalized above) short-circuits resolution.
            filters=active_filters,
        )

    # Meta gate — "what does this system do" → answer from trusted system state,
    # then one bounded guidance turn; no retrieval. This sits AFTER the
    # scope-warning check and BEFORE entity
    # resolution/retrieval ON PURPOSE. It is a HARD VETO: fire meta only when the
    # phrase matches AND the question does NOT resolve to a named in-corpus drug.
    # The ordering is load-bearing — a named-drug question that happens to carry a
    # meta phrase ("what BE study do you cover for atorvastatin?") MUST skip meta
    # and continue to the grounded cite-or-refuse path, never the uncited answer.
    # A caller-pinned product (API/dossier filter) is likewise a resolved context,
    # so it also skips meta.
    if (
        _is_meta_request(question)
        and not active_filters.get("normalized_name")
        and resolve_product(question).status != "resolved"
    ):
        return _decline(_meta, reason="meta", response_mode="meta")

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
            return _decline(
                _clarify,
                reason="ambiguous_product",
                response_mode="clarify",
                interpretation="More than one product matches that. Which did you mean?",
                options=_options_from_names(resolution.candidates),
            )
        else:
            session_filters = _session_filters()
            if session_filters.get("normalized_name") and _looks_like_follow_up(question):
                # Carry the product across turns (the chosen dosage_form/route are
                # carried just below, after resolved_name is set, so the same logic
                # also covers the single-product-corpus fallback path).
                active_filters["normalized_name"] = canonical_name(
                    str(session_filters["normalized_name"])
                )
                context_applied = True
                resolved_by_name = False
            else:
                # No product named. Offer a high-confidence "did you mean" for genuine
                # typos, then a brand→generic lookup (Adderall → amphetamine); else
                # refuse (e.g. romidepsin — absent, a deliberate must-refuse).
                suggestions = suggest_products(question)
                if suggestions:
                    return _decline(
                        _clarify,
                        reason="did_you_mean",
                        response_mode="clarify",
                        interpretation="I couldn't find that exact drug. Did you mean:",
                        options=_options_from_names(suggestions),
                        related=_options_from_names(suggestions),
                    )
                brand_matches = resolve_brand(question)
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
                return _decline(
                    _refuse,
                    reason="no_product",
                    response_mode="refused",
                    passages=[],
                    # Resolver candidates already computed above. Both are []
                    # on this branch (a genuinely-absent drug, e.g. romidepsin)
                    # -- so `related` is [] and the path never crashes.
                    related=_options_from_names(suggestions + brand_matches),
                )

    resolved_name = active_filters.get("normalized_name")

    # Multi-form session carry-over: a follow-up that didn't itself pin a form
    # inherits the dosage_form/route the user already chose for THIS product (via
    # a prior multi-form clarify). Done here — after resolution — so it also covers
    # the single-product-corpus fallback, where the resolver re-pins the product
    # and the `none`-branch carry-over above never runs. Without it the next
    # "What about dissolution?" would re-trigger the multi-form clarify.
    if (
        resolved_name
        and not active_filters.get("dosage_form")
        and not active_filters.get("route")
        and _looks_like_follow_up(question)
    ):
        session_filters = _session_filters()
        if session_filters.get("normalized_name") == resolved_name:
            for key in ("dosage_form", "route"):
                if session_filters.get(key):
                    active_filters[key] = session_filters[key]
                    context_applied = True

    # Bare drug name / no real question → guide with options instead of dumping a
    # default BE answer. Fires however the product was pinned — named in the
    # question, an API/UI filter, or session carry-over — so a no-topic input
    # ("Hello" with an Active-ingredient filter) never reaches the synthesizer
    # and comes back as a cited greeting. This deterministic guard owns the
    # options and status before the bounded guidance planner sees the turn.
    if resolved_name and _looks_vague(question, resolved_name):
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
    # a wrong-form PSG be cited as if it answered the question — and the blend is
    # invisible because citation labels are appl-number-only. So once a product is
    # resolved (by name OR by an API/UI filter), enumerate its CURRENT documents'
    # distinct (dosage_form, route) combos, honoring any form/route already pinned;
    # if more than one remains, CLARIFY which form before retrieving. One audit row.
    if resolved_name:
        try:
            combos = current_dosage_form_routes(
                resolved_name,
                dosage_form=active_filters.get("dosage_form"),
                route=active_filters.get("route"),
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
            # pin it and proceed — a form-explicit question shouldn't pay a clarify
            # hop (and on the full catalog it would otherwise flip answerable items
            # to clarify). Only a form-silent or ambiguous question clarifies.
            pinned = _combo_from_question(question, combos)
            if pinned is not None:
                active_filters["dosage_form"], active_filters["route"] = pinned
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

    # Stage 1: wide-net vector search (up to VECTOR_TOP_K), constrained to the product.
    route_json = _route_json(
        filters=active_filters,
        reason="retrieval",
        context_applied=context_applied,
        response_mode=response_mode,
    )
    _emit("Searching the FDA guidance corpus…")
    _maybe_inject_fault("retrieve")
    passages = retrieve(question, k=k, filters=active_filters)
    # Stage 2: optional rerank, then trim to RERANK_TOP_K.
    passages = rerank_passages(question, passages)
    passages = passages[: s.effective_rerank_top_k]

    # INV-2: weak passages never enter grounded synthesis. The constrained
    # guidance model still sees the QUESTION and trusted route/options, but not
    # the sub-threshold passage text, so it can choose a useful next step without
    # letting irrelevant evidence become citation cover. Gate on the
    # MAX cosine score, not passages[0]: the reranker (when enabled) reorders by
    # a cross-encoder score on a different scale, so passages[0].score may be a
    # demoted-but-still-present passage's cosine value — the 0.30 threshold is
    # calibrated against the cosine scale, so compare the true best cosine.
    if not passages or max(p.score for p in passages) < s.refusal_score_threshold:
        return _decline(
            _refuse,
            reason="low_top_score",
            response_mode="refused",
            passages=passages,
            # Surface the sub-threshold matches as inert "related" pointers
            # (distinct product NAMES + source link only). refused/citations
            # stay untouched — this never dresses the refusal as an answer.
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
    # the resolver. Mixed products → CLARIFY which (offer the distinct products)
    # rather than cite across them or bluntly refuse — the evidence is unclear,
    # so ask. Zero citations either way (never fabricates).
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
    # that are answerable together — e.g. same-combo beclomethasone docs).
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

    _emit(f"Reading {len(evidence_passages)} matching guidance passage(s)…")
    # Conversational memory: thread the last few ANSWERED turns so a follow-up
    # ("what about the fed study?") resolves naturally. Context ONLY — citations
    # are stripped (_format_recent) and the system prompt forbids treating it as a
    # source; INV-1 still holds because _validate_citations accepts only THIS
    # turn's passages, so a fact that lived only in a prior turn cannot acquire a
    # valid citation here and is dropped/refused. The current turn's just-written
    # user row is excluded by turn_id (the shell bakes that into the loader).
    # With no usable history the block is "" so the prompt is byte-identical to
    # the single-turn form (protects the eval).
    recent_block = _format_recent(load_recent_turns())
    recent_context = (
        "Recent conversation (context ONLY — use it to resolve pronouns and "
        "ellipsis in the question; it is NOT a source and MUST NOT be cited or "
        "treated as fact):\n<untrusted_recent_conversation>\n"
        f"{recent_block}\n</untrusted_recent_conversation>\n\n"
        if recent_block
        else ""
    )
    user_prompt = GROUNDED_QA_USER.format(
        recent_context=recent_context,
        question=question,
        passages=_format_passages(evidence_passages),
    )
    route_json["prompt"] = GROUNDED_QA_PROMPT.as_dict()

    _emit("Composing a cited answer…")
    log.info("llm_prompt", role="synthesizer", **GROUNDED_QA_PROMPT.log_fields())
    provider = get_llm_provider(role="synthesizer")
    synth_messages = [
        LLMMessage(role="system", content=GROUNDED_QA_SYSTEM),
        LLMMessage(role="user", content=user_prompt),
        # The schema rides as a TRAILING system message, so it is the last thing
        # the model reads and it never has to survive a .format() pass.
        TURN_SCHEMA_MESSAGE,
    ]
    try:
        response = _complete_structured(
            provider, synth_messages, max_tokens=s.synthesizer_max_tokens
        )
    except Exception as exc:  # provider transport error (timeout / 429 / 5xx)
        # B2: a synthesizer failure must NOT return a naked 500 with no audit
        # row — that would break INV-6 exactly when the system misbehaves. We
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
            guide=False,
        )
    answer = response.text.strip()

    # INV-1/INV-2: a degenerate completion (empty after stripping — e.g. a
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

    _emit("Checking each claim against its source…")
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
            guide=False,
        )

    log.info("qa_turn_gate", **_gate_log_fields(admitted))

    # OD-5's operator half rides on EVERY post-gate audit row, not just the
    # answer path. The decline branches below are exactly the turns where a
    # claim was DROPPED (no_valid_citations, material_drop), so persisting the
    # ledger only on the answer path would leave the per-claim drop reason and
    # the offending (short_name, page) pairs in a structlog line and nowhere in
    # the DB -- the opposite of what turn_gate.ledger claims to provide. Built
    # once here so the answer and decline paths cannot drift.
    turn_route = {
        "turn": tg.ledger(admitted, model=response.model, prompt_version=GROUNDED_QA_PROMPT.version)
    }

    if admitted.verdict == tg.VERDICT_NO_EVIDENCE:
        # The model declined. Unchanged two-way branch: the user named a real
        # drug but the model couldn't answer this phrasing (the live net for
        # vague inputs `_looks_vague` didn't catch) -> guide. When the product
        # came from the single-product fallback (no drug named), a decline is a
        # genuine "not covered" -> stay refused (INV-2).
        if resolved_by_name and resolved_name:
            return _decline(
                _clarify,
                reason="model_refusal",
                response_mode="clarify",
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

    rendered_answer = tg.render_answer(admitted)
    citations = tg.citations(admitted)
    route_json["partial_evidence"] = bool(admitted.unsupported)
    route_json.update(turn_route)

    audited = _audit_retrieved(passages)
    outcome = RagOutcome(
        answer=rendered_answer,
        citations=citations,
        refused=False,
        model_name=response.model,
        retrieved=audited,
        status=response_mode,
        reason=route_json.get("reason"),
        session_id=session_id,
        turn_id=turn_id,
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
        status=response_mode,
        route_json=route_json,
        # No-audit-no-answer (INV-6): a validated answer with no audit row is
        # never returned -- but the failure must be DEFINED, not a naked 500
        # that the stream-fallback client re-runs (a second paid synthesis)
        # into the same down DB. allow_skip=False makes the shell use the
        # STRICT write; on failure it serves this fallback -- the fixed-copy
        # status="error" refusal, whose own audit is re-attempted and skipped
        # (flagged) if the DB is still down. Built eagerly (it is pure):
        # active_filters/context_applied no longer mutate after synthesis, so
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
    return outcome, audit, _build_patch(outcome, filters=active_filters, route_json=route_json)


def ask(
    question: str,
    *,
    filters: dict[str, Any] | None = None,
    k: int | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    turn_id: str | None = None,
    bind_session: bool = True,
    on_progress: Callable[[str], None] | None = None,
    on_token: Callable[[str], None] | None = None,
) -> QAResult:
    """Grounded Q&A entry point — answer with citations, clarify, or refuse.

    The thin persistence SHELL around ``ask_core``: it owns the session ids and
    every write (user message, audit row, assistant message, filter carry-over)
    while the core owns every decision. A future control plane replaces this
    function without touching the core.

    ``bind_session=False`` keeps ``user_id`` as audit-only attribution (INV-6):
    the bookkeeping ChatSession stays unowned (user_id NULL) and so invisible
    to /sessions — for internal callers like the dossier, whose synthetic Q&A
    must not appear in the caller's chat history.

    ``on_progress`` (optional) receives short, cosmetic phase strings as the
    pipeline advances, for a live status ticker (POST /query/stream). It carries
    NO answer text or citations — INV-1 lives entirely in the post-validation
    answer path — and a failing sink can never break or slow the query.

    ``on_token`` (optional) receives the FINAL answer text in chunks, for a live
    "typing" effect. It fires only after the audit row is committed and only on
    an answer/summary turn, so every byte it emits is gated, rendered and
    audited — a declined or retracted draft can never reach it. A missing sink
    changes nothing else about the turn.
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
        session_id = ensure_session(session_id, user_id=user_id if bind_session else None)
        turn_id = turn_id or new_turn_id()
        record_message(
            session_id=session_id,
            turn_id=turn_id,
            role="user",
            content=question,
            filters=filters,
        )
    except SessionOwnershipError:
        # Lost an ownership race after the API's pre-check — abort rather than
        # write this caller's turns into another user's session (the API maps
        # this to its ownership 404).
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
