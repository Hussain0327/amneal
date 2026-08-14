"""Turn recorded route-shadow rows into the Checkpoint 3 evidence bundle.

Checkpoint 3 (docs/SLM_LAYER_IMPLEMENTATION_PLAN_2026-08-07.md:197-199) asks for
a joint mode/scope confusion matrix, added latency p95, QPS headroom, route
failure rate, and zero unsafe corpus authorizations in reviewed shadow traces.
The shadow has been writing every input that answer needs since PR11b, and
nothing has ever read it back out; the promotion decision was blocked on
arithmetic nobody had written.

This module is PURE. It takes already-loaded ``route_json["route_call"]``
mappings and returns a report. The database read lives in the CLI command, so
the acceptance arithmetic can be tested against fixtures instead of a prod
window, and so a reviewer can rerun it over an exported window offline.

Deliberately reports rather than judges, with one exception: ``unsafe_corpus``
counts authorizations that violate the corpus contract outright, because
"zero unsafe corpus authorizations" is a Checkpoint 3 acceptance line and a
reviewer should not have to eyeball it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# One nearest-rank percentile for the whole eval package, so a p95 printed here
# and a p95 printed on an eval scorecard mean the same arithmetic.
from regwatch.eval.metrics import percentile

# Checkpoint 3 acceptance: parse failure under 2% of attempted route calls
# (plan doc :186-188). Expressed as a fraction of attempts, not of successes.
PARSE_FAILURE_CEILING = 0.02

#: The bounded-set cap the compiler enforces (retrieve/scope.py).
_HARD_MAX_CORPUS_DOCUMENTS = 512

#: Reasons that legitimately authorize a corpus scope. Any other reason on a
#: compiled corpus decision means the compiler let something through that the
#: #163 safety contract forbids.
_AUTHORIZED_CORPUS_REASONS = frozenset({"explicit_corpus", "inherited_corpus"})


@dataclass(frozen=True)
class LatencyProfile:
    """Observed route-call latency. All values in milliseconds."""

    count: int
    p50: float | None = None
    p95: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class UnsafeCorpus:
    """One corpus authorization that violates the #163 contract."""

    reason: str
    violation: str
    document_count: int


@dataclass(frozen=True)
class RouteShadowReport:
    """The Checkpoint 3 evidence bundle, as arithmetic over shadow rows."""

    total_rows: int = 0
    attempted: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    failure_rate: float | None = None
    parse_failure_rate: float | None = None
    latency: LatencyProfile = LatencyProfile(count=0)
    #: (proposed mode, current mode) -> count. Successful compiles only.
    mode_matrix: dict[tuple[str, str], int] = field(default_factory=dict)
    #: (compiled scope kind, current scope) -> count. Successful compiles only.
    scope_matrix: dict[tuple[str, str], int] = field(default_factory=dict)
    mode_agreement: float | None = None
    scope_agreement: float | None = None
    compile_statuses: dict[str, int] = field(default_factory=dict)
    corpus_authorizations: int = 0
    unsafe_corpus: tuple[UnsafeCorpus, ...] = ()
    #: Scope-compiler reasons, the tuning surface for the corpus-intent rule.
    scope_reasons: dict[str, int] = field(default_factory=dict)
    #: Rows whose configured mode was the reserved "live" (executes as shadow).
    configured_live: int = 0
    total_cost_usd: float = 0.0

    @property
    def meets_parse_ceiling(self) -> bool:
        """Whether parse failures clear Checkpoint 3's under-2% acceptance."""
        return self.parse_failure_rate is not None and (
            self.parse_failure_rate < PARSE_FAILURE_CEILING
        )

    @property
    def has_unsafe_corpus(self) -> bool:
        """Whether any corpus authorization violated the #163 contract."""
        return bool(self.unsafe_corpus)


def _latency(rows: Iterable[Mapping[str, Any]]) -> LatencyProfile:
    observed = sorted(
        float(row["latency_ms"])
        for row in rows
        if isinstance(row.get("latency_ms"), int | float)
        and not isinstance(row["latency_ms"], bool)
    )
    if not observed:
        return LatencyProfile(count=0)
    return LatencyProfile(
        count=len(observed),
        p50=percentile(observed, 50),
        p95=percentile(observed, 95),
        maximum=observed[-1],
    )


def _corpus_violation(compiled: Mapping[str, Any]) -> str | None:
    """Why this corpus authorization is unsafe, or None when it is sound.

    Mirrors the contract in issue #163: a corpus route is allowed only when
    corpus intent was positively identified and an allowlisted policy expanded
    to a bounded, non-empty set of current document versions.
    """
    reason = str(compiled.get("reason") or "")
    documents = compiled.get("corpus_documents")
    count = len(documents) if isinstance(documents, list | tuple) else 0
    if reason not in _AUTHORIZED_CORPUS_REASONS:
        return f"corpus scope compiled under reason={reason!r}"
    if not compiled.get("corpus_policy"):
        return "corpus scope carries no allowlisted policy"
    if count == 0:
        return "corpus scope expanded to an empty document set"
    if count > _HARD_MAX_CORPUS_DOCUMENTS:
        return f"corpus scope expanded to {count} documents, above the {_HARD_MAX_CORPUS_DOCUMENTS} cap"
    return None


def summarize(rows: Iterable[Mapping[str, Any]]) -> RouteShadowReport:
    """Aggregate ``route_json["route_call"]`` mappings into the evidence bundle.

    Args:
        rows: One mapping per turn that attempted a route call. Rows missing
            ``attempted`` are counted in ``total_rows`` but excluded from every
            rate, so a window that mixes route-off turns in does not silently
            dilute the failure rate.

    Returns:
        The Checkpoint 3 evidence bundle. Empty input yields a report whose
        rates are ``None`` rather than zero -- "no data" and "no failures" must
        not look alike on a promotion decision.
    """
    rows = list(rows)
    attempted = [row for row in rows if row.get("attempted")]
    outcomes = Counter(str(row.get("outcome") or "unknown") for row in attempted)
    compile_statuses = Counter(
        str(row.get("compile_status")) for row in attempted if row.get("compile_status")
    )

    mode_matrix: Counter[tuple[str, str]] = Counter()
    scope_matrix: Counter[tuple[str, str]] = Counter()
    scope_reasons: Counter[str] = Counter()
    unsafe: list[UnsafeCorpus] = []
    corpus_authorizations = 0

    for row in attempted:
        if row.get("compile_status") != "success":
            continue
        compiled = row.get("compiled_scope") or {}
        mode_matrix[(str(row.get("mode") or "?"), str(row.get("current_mode") or "?"))] += 1
        scope_matrix[(str(compiled.get("kind") or "?"), str(row.get("current_scope") or "?"))] += 1
        if compiled.get("reason"):
            scope_reasons[str(compiled["reason"])] += 1
        if str(compiled.get("kind")) == "corpus":
            corpus_authorizations += 1
            violation = _corpus_violation(compiled)
            if violation is not None:
                documents = compiled.get("corpus_documents")
                unsafe.append(
                    UnsafeCorpus(
                        reason=str(compiled.get("reason") or ""),
                        violation=violation,
                        document_count=(
                            len(documents) if isinstance(documents, list | tuple) else 0
                        ),
                    )
                )

    agreed_mode = [row for row in attempted if row.get("agrees_with_mode") is not None]
    agreed_scope = [row for row in attempted if row.get("agrees_with_scope") is not None]

    return RouteShadowReport(
        total_rows=len(rows),
        attempted=len(attempted),
        outcomes=dict(outcomes),
        failure_rate=(
            (len(attempted) - outcomes.get("success", 0)) / len(attempted) if attempted else None
        ),
        parse_failure_rate=(outcomes.get("invalid", 0) / len(attempted) if attempted else None),
        latency=_latency(attempted),
        mode_matrix=dict(mode_matrix),
        scope_matrix=dict(scope_matrix),
        mode_agreement=(
            sum(1 for row in agreed_mode if row["agrees_with_mode"]) / len(agreed_mode)
            if agreed_mode
            else None
        ),
        scope_agreement=(
            sum(1 for row in agreed_scope if row["agrees_with_scope"]) / len(agreed_scope)
            if agreed_scope
            else None
        ),
        compile_statuses=dict(compile_statuses),
        corpus_authorizations=corpus_authorizations,
        unsafe_corpus=tuple(unsafe),
        scope_reasons=dict(scope_reasons),
        configured_live=sum(1 for row in attempted if row.get("configured_mode") == "live"),
        total_cost_usd=sum(
            float(row["cost_usd"])
            for row in attempted
            if isinstance(row.get("cost_usd"), int | float)
            and not isinstance(row["cost_usd"], bool)
        ),
    )
