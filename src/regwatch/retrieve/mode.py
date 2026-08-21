"""Explicit retrieval modes.

Until this module existed, the retrieval ALGORITHM was chosen by a side effect:
``similarity_search_profile`` branched on ``bool(clause)``, so "did a metadata
filter happen to be present" decided exact-vs-approximate search. That is not a
property anyone chose, it is not recorded anywhere, and on a regulatory-evidence
path it means the planner can decide which document gets cited.

Two facts make the implicit rule actively misleading:

* ``retrieve()`` appends a current-version clause to EVERY query, so the clause
  is non-empty on 100% of production turns. "Filtered" therefore carries no
  information -- an unscoped corpus-wide question and a question pinned to one
  drug are indistinguishable by that test, even though they were measured at
  hit@8 of 51.2% and 93.0% respectively.
* An approximate index cannot be made exact by ordering. Measured against exact
  ground truth on this corpus, HNSW recall@8 averaged 0.984 but bottomed out at
  0.125 on one query -- usually perfect, occasionally catastrophic, and silent
  either way.

So the mode is named, passed explicitly, and recorded. ``EXACT_SCOPED`` and
``EXACT_CORPUS`` emit byte-identical SQL; the distinction is a POLICY label
recording whether a product actually resolved, which is the thing the old
``bool(clause)`` test could never express.

This module is pure: no SQL, no I/O, no store imports. It is the vocabulary the
store and the retriever agree on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Metadata keys that mean "a specific product was resolved". A version clause
# does NOT count -- retriever.py adds one unconditionally.
_PRODUCT_SCOPE_KEYS = frozenset({"normalized_name", "doc_id", "appl_no"})


class RetrievalMode(StrEnum):
    """How a search is executed. Chosen by the caller, never inferred."""

    #: Exact scan over a product-scoped set. The compliance-sensitive path.
    EXACT_SCOPED = "exact_scoped"
    #: Exact scan over the whole current-version corpus (no product resolved).
    EXACT_CORPUS = "exact_corpus"
    #: HNSW candidate generation followed by an exact full-precision rerank.
    #: Opt-in only, and never with a metadata filter -- pgvector applies a
    #: filter AFTER the approximate scan, which silently drops matches.
    ANN_RERANKED = "ann_reranked"

    @property
    def is_exact(self) -> bool:
        return self in (RetrievalMode.EXACT_SCOPED, RetrievalMode.EXACT_CORPUS)


class ScopeNotResolved(ValueError):
    """EXACT_SCOPED was requested but no product filter is present."""


class ApproximateSearchNotPermitted(ValueError):
    """Approximate vector search is disabled for RegWatch retrieval."""


@dataclass(frozen=True)
class RetrievalScope:
    """What the query was narrowed to, independent of how it was executed."""

    product_pinned: bool
    version_pinned: bool
    filter_keys: tuple[str, ...]
    scope_version_count: int | None = None

    @classmethod
    def from_filters(
        cls,
        filters: dict[str, Any] | None,
        *,
        scope_version_count: int | None = None,
    ) -> RetrievalScope:
        keys = tuple(sorted(k for k, v in (filters or {}).items() if v not in (None, "", [])))
        return cls(
            product_pinned=any(k in _PRODUCT_SCOPE_KEYS for k in keys),
            version_pinned="version_id" in keys,
            filter_keys=keys,
            scope_version_count=scope_version_count,
        )


def default_mode_for_scope(scope: RetrievalScope) -> RetrievalMode:
    """The mode a caller gets when it does not care to choose.

    Exact either way -- the approximate path is never a default. Which of the
    two exact modes is a policy label, so the audit row can tell an unscoped
    corpus sweep apart from a properly scoped lookup.
    """
    return RetrievalMode.EXACT_SCOPED if scope.product_pinned else RetrievalMode.EXACT_CORPUS


def assert_mode_permitted(mode: RetrievalMode, scope: RetrievalScope) -> None:
    """Fail closed on a mode/scope combination that cannot mean what it says."""
    if mode is RetrievalMode.EXACT_SCOPED and not scope.product_pinned:
        raise ScopeNotResolved(
            "EXACT_SCOPED requires a resolved product filter "
            f"(one of {sorted(_PRODUCT_SCOPE_KEYS)}); got filter keys {list(scope.filter_keys)}"
        )
    if mode is RetrievalMode.ANN_RERANKED:
        raise ApproximateSearchNotPermitted(
            "ANN_RERANKED is disabled: RegWatch requires exact pgvector search "
            f"for every scope; got filter keys {list(scope.filter_keys)}"
        )


@dataclass(frozen=True)
class RetrievalPlan:
    """What actually ran. Recorded on the audit row (INV-6)."""

    mode: RetrievalMode
    scope: RetrievalScope
    profile_id: str
    dimension: int
    k: int
    returned: int = 0
    #: Exact modes disable index scans; ANN sets ef_search and a candidate pool.
    enable_indexscan: bool = False
    ef_search: int | None = None
    candidate_k: int | None = None
    #: The ORDER BY suffix that makes ties deterministic, or "" on the inner
    #: ANN candidate ordering where a second key would prevent index use.
    tiebreak: str = ""
    index_dtype: str = "vector"
    extras: dict[str, Any] = field(default_factory=dict)

    def with_result(self, returned: int) -> RetrievalPlan:
        return RetrievalPlan(
            mode=self.mode,
            scope=self.scope,
            profile_id=self.profile_id,
            dimension=self.dimension,
            k=self.k,
            returned=returned,
            enable_indexscan=self.enable_indexscan,
            ef_search=self.ef_search,
            candidate_k=self.candidate_k,
            tiebreak=self.tiebreak,
            index_dtype=self.index_dtype,
            extras=self.extras,
        )

    def as_route_json(self) -> dict[str, Any]:
        """The block recorded under ``query_log.route_json['retrieval']``.

        JSON rather than new columns: route_json is already JSONB and already
        flows through the Go control plane as an opaque payload
        (go/internal/api/ragclient.go carries RouteJson as json.RawMessage), so
        this ships on both control planes with no migration and no Go change.
        """
        return {
            "mode": self.mode.value,
            "exact": self.mode.is_exact,
            "product_pinned": self.scope.product_pinned,
            "filter_keys": list(self.scope.filter_keys),
            "scope_version_count": self.scope.scope_version_count,
            "profile_id": self.profile_id,
            "dimension": self.dimension,
            "k": self.k,
            "returned": self.returned,
            "enable_indexscan": self.enable_indexscan,
            "ef_search": self.ef_search,
            "candidate_k": self.candidate_k,
            "tiebreak": self.tiebreak,
            "index_dtype": self.index_dtype,
            **self.extras,
        }
