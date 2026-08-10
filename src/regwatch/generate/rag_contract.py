"""Contract between the stateless RAG core and its persistence shell.

Step 2 of the strangler migration: ``grounded_qa.ask_core`` computes a turn
and RETURNS what to persist; the ``ask()`` shell (today Python, later the Go
control plane) owns every write. These dataclasses are that boundary. They
are plain stdlib dataclasses -- ``asdict()``-able, JSON-serializable -- because
they are designed to become a cross-service HTTP contract; nothing here may
import the pipeline, the store, or pydantic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# The FULL status vocabulary ask() can emit. Defined here (the domain layer)
# and re-exported by grounded_qa for api.main's wire model, so the OpenAPI
# enum -- and the TS union generated from it (lib/api-types.ts) -- can never
# drift from what the domain actually returns (dependencies point inward).
QueryStatusLiteral = Literal[
    "answer", "summary", "clarify", "scope_warning", "meta", "refused", "error"
]


@dataclass
class Citation:
    short_name: str
    page: int
    chunk_id: str
    doc_id: int
    version_id: int
    source_url: str
    snippet: str
    # Tier-2 confidence: the retriever similarity score of the passage this
    # citation traces to, copied from the matching retrieved passage by
    # chunk_id (never recomputed). None when no retrieved passage matches --
    # e.g. a deterministic/uncited path that emits no retrieval. Purely
    # additive context; INV-1 is unaffected (the citation still traces to a
    # sent passage -- this just annotates it with that passage's score).
    score: float | None = None


@dataclass
class ClarifyOption:
    """A clickable follow-up: a plain-language label + the query to resubmit."""

    label: str
    query: str
    filters: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClaimTag:
    """What the gate ADMITTED, per rendered sentence: its epistemic kind and
    whether the renderer stamped a citation on it. Python-internal: never
    enters QueryResponse (api/main.py names every wire field), never
    persisted (the ledger already carries kind on turn_gate.AdmittedClaim)."""

    kind: str  # turn_gate.CLAIM_KIND_SOURCE_FACT | "reasoning" | "conversation"
    cited: bool


@dataclass
class RagOutcome:
    """The COMPUTE result of one turn.

    Everything ``QAResult`` carries except the audit id, which does not exist
    until the shell writes the audit row (INV-6 stays a shell concern).
    """

    answer: str
    citations: list[Citation]
    refused: bool
    model_name: str
    retrieved: list[dict[str, Any]]
    session_id: str
    turn_id: str
    status: QueryStatusLiteral = "answer"
    reason: str | None = None
    interpretation: str | None = None
    clarify: list[ClarifyOption] = field(default_factory=list)
    related: list[ClarifyOption] = field(default_factory=list)
    # Eval-only: the gate's per-claim (kind, cited) ledger, for
    # eval.metrics.faithfulness's kind-aware denominator. Set only on the
    # answer path (grounded_qa.py); every decline branch leaves this at its
    # default, which the metric reads as "untagged" and scores with the
    # pre-redefinition text rule (eval/metrics.py).
    claim_tags: tuple[ClaimTag, ...] = ()


@dataclass
class SessionPatch:
    """Chat-history mutations the shell applies AFTER the audit write.

    The assistant message references the audit row, so ``audit_id`` is not a
    field here: the shell injects it when applying the patch (it is the
    apply-time parameter, never core state). The USER-message write is not
    part of the patch either -- it happens in the shell before compute,
    exactly as before, so a core exception still leaves the question in the
    chat history.
    """

    session_id: str
    turn_id: str
    content: str
    status: str
    model_name: str
    reason: str | None
    interpretation: str | None
    filters: dict[str, Any]
    citations: list[dict[str, Any]]
    clarify: list[dict[str, Any]]
    related: list[dict[str, Any]]
    metadata: dict[str, Any]
    # Precomputed in the core (domain rule: only an answer/summary/clarify
    # turn with a pinned product updates the session's carry-over filters) so
    # the shell applies mutations without re-deriving policy.
    update_filters: bool


@dataclass
class AuditPayload:
    """Every argument the shell's ``log_query`` call needs, for every branch.

    ``allow_skip`` carries the branch's failure semantics: non-answer terminal
    paths (refuse/clarify/meta) audit via the defined-failure wrapper (-1 on a
    failed write, trusted fallback still returned), while the validated-answer
    path is STRICT -- no-audit-no-answer (INV-6) -- and degrades to
    ``failure_fallback`` (the fixed-copy status="error" refusal turn, itself
    skip-audited) when the write fails.
    """

    mode: str
    query_text: str
    retrieved: list[dict[str, Any]]
    answer_text: str
    citations: list[dict[str, Any]]
    refused: bool
    model_name: str
    session_id: str
    turn_id: str
    user_id: str | None
    status: str
    route_json: dict[str, Any]
    # Token/cost columns (H3). None keeps them NULL -- log_query's own
    # defaults -- so an absent LLM call stays NULL and an unpriced model's cost
    # stays NULL, never guessed.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    allow_skip: bool = True
    failure_fallback: tuple[RagOutcome, AuditPayload, SessionPatch] | None = None

    def log_kwargs(self) -> dict[str, Any]:
        """Kwargs for ``log_query``, with the failure semantics stripped."""
        return {
            "mode": self.mode,
            "query_text": self.query_text,
            "retrieved": self.retrieved,
            "answer_text": self.answer_text,
            "citations": self.citations,
            "refused": self.refused,
            "model_name": self.model_name,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "user_id": self.user_id,
            "status": self.status,
            "route_json": self.route_json,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
        }
