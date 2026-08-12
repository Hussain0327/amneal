"""Deterministic compilation of advisory route hints into executable scope.

The route model cannot call this boundary with self-authored filters. Callers
must supply product filters from deterministic resolution, corpus membership
from the current-version catalog, and prior corpus scope from an audited turn.
The module is pure: no database, store, session, or model access.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeAlias

from regwatch.generate.route import CorpusPolicyHint, RouteDecision, ScopeHint, TurnMode
from regwatch.retrieve.mode import RetrievalMode

HARD_MAX_CORPUS_DOCUMENTS = 512
_PRODUCT_IDENTITY_KEYS = frozenset({"normalized_name", "doc_id", "appl_no"})
_PRODUCT_FILTER_KEYS = frozenset(
    {"normalized_name", "doc_id", "appl_no", "dosage_form", "route", "psg_type"}
)

FilterScalar: TypeAlias = str | int | float
FrozenFilter: TypeAlias = tuple[str, FilterScalar]


@dataclass(frozen=True)
class ProductScope:
    """The product filters one turn executes under, as a value not a bare dict.

    A LOSSLESS carrier. It never drops, merges, renames, or infers a key, and it
    applies no type or key whitelist -- whatever the turn resolved is what
    retrieval sees. Its only normalization is dropping ``None``/``""``/``[]``,
    which ``retriever._build_where`` and ``conversation._safe_filters`` already
    do identically, so a scope built from a turn's filters yields the same WHERE
    clause as the dict it replaced. That equivalence is the whole point: it is
    what lets this type sit on the retrieval boundary without moving behaviour.

    Casing is deliberately NOT normalized here. ``retriever._fold_filter_casing``
    owns the mapping from a user's casing to the corpus's stored casing, and
    folding early would defeat it -- a title-cased value would find no stored
    match, filter to nothing, and read as an INV-2 refusal on an answerable
    question.

    Distinct from ``CompiledScope``, which is the ROUTE lane's authorized result
    and carries route vocabulary (``ScopeSource``/``ScopeReason``). Ordinary
    product turns run with the route call off, so they have no such vocabulary
    to report and must not borrow one.
    """

    filters: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def from_filters(cls, filters: Mapping[str, Any] | None) -> ProductScope:
        """Build a scope from a turn's filter mapping, dropping empty values.

        Source order is preserved, NOT sorted. ``retriever._build_where`` emits
        a multi-key filter as an ordered ``{"$and": [...]}`` list, so sorting
        here would reorder that list and change the generated SQL for turns that
        pin more than one filter. Semantically equivalent, but this refactor
        promised byte-identical behaviour, and a reordered clause is not that.
        """
        active = {k: v for k, v in (filters or {}).items() if v not in (None, "", [])}
        return cls(filters=tuple(active.items()))

    def as_filters(self) -> dict[str, Any]:
        """The filter mapping to hand to the store. A fresh dict every call."""
        return dict(self.filters)

    @property
    def normalized_name(self) -> str | None:
        """The pinned product, or ``None`` when the turn resolved no product."""
        value = dict(self.filters).get("normalized_name")
        return str(value) if value else None

    @property
    def is_resolved(self) -> bool:
        """Whether a product is pinned. Mirrors the truthiness test it replaces."""
        return self.normalized_name is not None


class CompiledScopeKind(StrEnum):
    PRODUCT = "product"
    CORPUS = "corpus"
    CLARIFY = "clarify"
    CONVERSE = "converse"


class ScopeSource(StrEnum):
    NONE = "none"
    RESOLVED_PRODUCT = "resolved_product"
    SESSION_PRODUCT = "session_product"
    EXPLICIT_CORPUS = "explicit_corpus"
    AUDITED_CORPUS = "audited_corpus"


class ScopeReason(StrEnum):
    CONVERSE = "converse"
    RESOLVED_PRODUCT = "resolved_product"
    SESSION_PRODUCT = "session_product"
    EXPLICIT_CORPUS = "explicit_corpus"
    INHERITED_CORPUS = "inherited_corpus"
    PRODUCT_NOT_RESOLVED = "product_not_resolved"
    SCOPE_UNKNOWN = "scope_unknown"
    CORPUS_INTENT_NOT_EXPLICIT = "corpus_intent_not_explicit"
    CORPUS_POLICY_MISMATCH = "corpus_policy_mismatch"
    CORPUS_POLICY_UNAVAILABLE = "corpus_policy_unavailable"
    CORPUS_POLICY_EMPTY = "corpus_policy_empty"
    CORPUS_POLICY_UNBOUNDED = "corpus_policy_unbounded"
    CORPUS_POLICY_STALE = "corpus_policy_stale"
    CORPUS_POLICY_INVALID = "corpus_policy_invalid"
    CORPUS_INHERITANCE_UNAUDITED = "corpus_inheritance_unaudited"


@dataclass(frozen=True, order=True)
class CorpusDocumentRef:
    """One current document/version/application association in a corpus policy."""

    doc_id: int
    version_id: int
    appl_no: str
    short_name: str
    is_current: bool = True

    def __post_init__(self) -> None:
        if self.doc_id <= 0 or self.version_id <= 0:
            raise ValueError("corpus document ids must be positive")
        if not self.appl_no.strip() or not self.short_name.strip():
            raise ValueError("corpus documents require application and source labels")

    def as_audit_json(self) -> dict[str, object]:
        return {
            "doc_id": self.doc_id,
            "version_id": self.version_id,
            "appl_no": self.appl_no,
            "short_name": self.short_name,
        }


@dataclass(frozen=True)
class CorpusPolicySnapshot:
    """Application-supplied expansion of an allowlisted policy at one instant."""

    policy: CorpusPolicyHint
    documents: tuple[CorpusDocumentRef, ...]
    max_documents: int = 64

    def validation_failure(self) -> ScopeReason | None:
        if not self.documents:
            return ScopeReason.CORPUS_POLICY_EMPTY
        if (
            self.max_documents <= 0
            or self.max_documents > HARD_MAX_CORPUS_DOCUMENTS
            or len(self.documents) > self.max_documents
        ):
            return ScopeReason.CORPUS_POLICY_UNBOUNDED
        if any(not document.is_current for document in self.documents):
            return ScopeReason.CORPUS_POLICY_STALE
        doc_ids = [document.doc_id for document in self.documents]
        version_ids = [document.version_id for document in self.documents]
        associations = [
            (document.doc_id, document.version_id, document.appl_no) for document in self.documents
        ]
        if (
            len(set(doc_ids)) != len(doc_ids)
            or len(set(version_ids)) != len(version_ids)
            or len(set(associations)) != len(associations)
        ):
            return ScopeReason.CORPUS_POLICY_INVALID
        return None


@dataclass(frozen=True)
class CompiledScope:
    """Application-authorized scope result, suitable for audit and later wiring."""

    kind: CompiledScopeKind
    source: ScopeSource
    reason: ScopeReason
    product_filters: tuple[FrozenFilter, ...] = ()
    corpus_policy: CorpusPolicyHint | None = None
    corpus_documents: tuple[CorpusDocumentRef, ...] = ()
    inherited_from_audit_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind is CompiledScopeKind.PRODUCT:
            if (
                not self.product_filters
                or self.corpus_policy is not None
                or self.corpus_documents
                or self.source not in {ScopeSource.RESOLVED_PRODUCT, ScopeSource.SESSION_PRODUCT}
            ):
                raise ValueError("invalid compiled product scope")
            if not (_PRODUCT_IDENTITY_KEYS & dict(self.product_filters).keys()):
                raise ValueError("compiled product scope requires a product identity")
        elif self.kind is CompiledScopeKind.CORPUS:
            if (
                self.product_filters
                or self.corpus_policy is None
                or not self.corpus_documents
                or self.source not in {ScopeSource.EXPLICIT_CORPUS, ScopeSource.AUDITED_CORPUS}
            ):
                raise ValueError("invalid compiled corpus scope")
            snapshot = CorpusPolicySnapshot(
                policy=self.corpus_policy,
                documents=self.corpus_documents,
                max_documents=HARD_MAX_CORPUS_DOCUMENTS,
            )
            if snapshot.validation_failure() is not None:
                raise ValueError("invalid compiled corpus membership")
        elif (
            self.product_filters
            or self.corpus_policy is not None
            or self.corpus_documents
            or self.source is not ScopeSource.NONE
        ):
            raise ValueError("non-retrieval scope cannot carry executable state")
        if self.source is ScopeSource.AUDITED_CORPUS:
            if self.inherited_from_audit_id is None or self.inherited_from_audit_id <= 0:
                raise ValueError("audited corpus scope requires a positive audit id")
        elif self.inherited_from_audit_id is not None:
            raise ValueError("only inherited corpus scope may carry an audit id")

    @property
    def retrieval_mode(self) -> RetrievalMode | None:
        if self.kind is CompiledScopeKind.PRODUCT:
            return RetrievalMode.EXACT_SCOPED
        if self.kind is CompiledScopeKind.CORPUS:
            return RetrievalMode.EXACT_CORPUS
        return None

    @property
    def should_update_product_session(self) -> bool:
        """Corpus and non-retrieval turns can never replace the active product."""
        return self.kind is CompiledScopeKind.PRODUCT

    @property
    def allowed_version_ids(self) -> tuple[int, ...]:
        """The exact corpus membership contract; empty outside CorpusScope."""
        return tuple(document.version_id for document in self.corpus_documents)

    def product_filter_dict(self) -> dict[str, FilterScalar] | None:
        if self.kind is not CompiledScopeKind.PRODUCT:
            return None
        return dict(self.product_filters)

    def as_retrieval_contract(self) -> dict[str, object] | None:
        """Describe future wiring without masquerading as current flat filters.

        The current retriever interprets a flat list value as equality. Corpus
        integration in PR12 must consume ``version_id_in`` as membership and
        then apply ``allows_corpus_passage`` to every returned passage.
        """
        if self.kind is CompiledScopeKind.PRODUCT:
            return {
                "mode": RetrievalMode.EXACT_SCOPED.value,
                "filters": dict(self.product_filters),
            }
        if self.kind is CompiledScopeKind.CORPUS:
            return {
                "mode": RetrievalMode.EXACT_CORPUS.value,
                "version_id_in": list(self.allowed_version_ids),
                "documents": [document.as_audit_json() for document in self.corpus_documents],
            }
        return None

    def allows_corpus_passage(
        self,
        *,
        doc_id: int,
        version_id: int,
        appl_no: str,
        short_name: str,
    ) -> bool:
        """Check the full provenance tuple, not version membership alone."""
        if self.kind is not CompiledScopeKind.CORPUS:
            return False
        return any(
            (
                document.doc_id == doc_id
                and document.version_id == version_id
                and document.appl_no == appl_no
                and document.short_name == short_name
            )
            for document in self.corpus_documents
        )

    def as_audit_json(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "source": self.source.value,
            "reason": self.reason.value,
            "retrieval_mode": self.retrieval_mode.value if self.retrieval_mode else None,
            "product_filters": dict(self.product_filters),
            "corpus_policy": self.corpus_policy.value if self.corpus_policy else None,
            "corpus_documents": [document.as_audit_json() for document in self.corpus_documents],
            "scope_version_count": len(self.corpus_documents),
            "inherited_from_audit_id": self.inherited_from_audit_id,
        }


def _normalize_question(question: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", question.lower()).split())


def detect_explicit_corpus_policy(question: str) -> CorpusPolicyHint | None:
    """Conservatively recognize the initial inhalation-PSG corpus policy.

    This reads the original user text. A route model cannot manufacture the
    cue by inserting "across" into its standalone rewrite.
    """
    text = _normalize_question(question)
    inhalation_family = any(
        phrase in text
        for phrase in (
            "inhalation product specific guidance",
            "metered dose inhalation aerosol guidance",
            "generic metered dose inhaler",
        )
    )
    cross_document = any(
        cue in text
        for cue in (
            "across the fda",
            "across all",
            "product specific guidances",
            "inhalation aerosol guidances",
            "proposed generic metered dose inhaler",
        )
    )
    if inhalation_family and cross_document:
        return CorpusPolicyHint.INHALATION_PSG
    return None


def _freeze_product_filters(
    filters: Mapping[str, object] | None,
) -> tuple[FrozenFilter, ...] | None:
    if not filters:
        return None
    active = {key: value for key, value in filters.items() if value not in (None, "", [])}
    if not active or not set(active).issubset(_PRODUCT_FILTER_KEYS):
        return None
    if not (_PRODUCT_IDENTITY_KEYS & active.keys()):
        return None
    frozen: list[FrozenFilter] = []
    for key, value in sorted(active.items()):
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            return None
        frozen.append((key, value))
    return tuple(frozen)


def _clarify(reason: ScopeReason) -> CompiledScope:
    return CompiledScope(
        kind=CompiledScopeKind.CLARIFY,
        source=ScopeSource.NONE,
        reason=reason,
    )


def _product_scope(
    filters: tuple[FrozenFilter, ...],
    *,
    source: ScopeSource,
    reason: ScopeReason,
) -> CompiledScope:
    return CompiledScope(
        kind=CompiledScopeKind.PRODUCT,
        source=source,
        reason=reason,
        product_filters=filters,
    )


def _corpus_scope(
    policy: CorpusPolicyHint,
    *,
    corpus_policies: Mapping[CorpusPolicyHint, CorpusPolicySnapshot] | None,
    source: ScopeSource,
    reason: ScopeReason,
    inherited_from_audit_id: int | None = None,
) -> CompiledScope:
    """Re-expand and validate current catalog membership for every corpus turn."""
    snapshot = (corpus_policies or {}).get(policy)
    if snapshot is None:
        return _clarify(ScopeReason.CORPUS_POLICY_UNAVAILABLE)
    if snapshot.policy is not policy:
        return _clarify(ScopeReason.CORPUS_POLICY_MISMATCH)
    failure = snapshot.validation_failure()
    if failure is not None:
        return _clarify(failure)
    return CompiledScope(
        kind=CompiledScopeKind.CORPUS,
        source=source,
        reason=reason,
        corpus_policy=policy,
        corpus_documents=tuple(sorted(snapshot.documents)),
        inherited_from_audit_id=inherited_from_audit_id,
    )


@dataclass(frozen=True)
class CorpusIntentProbe:
    """What the corpus branch would have concluded. Observation only.

    ``compile_scope`` returns a deterministic product scope the moment one
    resolves, even when the route model proposed CORPUS (see the precedence
    rule below). That is the correct authorization -- a resolved product is
    narrower and safer than a model's corpus guess -- but it means the audit row
    records ``reason=resolved_product`` and says nothing about whether the
    corpus intent was real. Every corpus-phrased question that also names a drug
    therefore lands in the shadow window as a silent false negative.

    This runs the corpus branch's PREDICATES without its authority, so the
    window can show how often product precedence masked a genuine corpus
    request. It never affects what compile_scope returns.
    """

    detected_policy: str | None
    would_be_reason: str
    #: True when compile_scope actually reached the corpus branch, so the probe
    #: merely restates the decision rather than revealing a masked one.
    reached_corpus_branch: bool

    def as_audit_json(self) -> dict[str, object]:
        return {
            "detected_policy": self.detected_policy,
            "would_be_reason": self.would_be_reason,
            "reached_corpus_branch": self.reached_corpus_branch,
        }


def probe_corpus_intent(
    decision: RouteDecision,
    *,
    original_question: str,
    resolved_product_filters: Mapping[str, object] | None = None,
) -> CorpusIntentProbe | None:
    """Evaluate the corpus predicates for observation, authorizing nothing.

    Mirrors ``compile_scope``'s corpus branch minus the catalog expansion, which
    needs I/O and cannot change whether intent was explicit. Returns None when
    the route did not propose a corpus scope, so the audit stays quiet on turns
    this question does not apply to.
    """
    if decision.mode is TurnMode.CONVERSE or decision.scope_hint is not ScopeHint.CORPUS:
        return None
    detected = detect_explicit_corpus_policy(original_question)
    if detected is None:
        reason = ScopeReason.CORPUS_INTENT_NOT_EXPLICIT
    elif detected is not decision.corpus_policy_hint:
        reason = ScopeReason.CORPUS_POLICY_MISMATCH
    else:
        reason = ScopeReason.EXPLICIT_CORPUS
    return CorpusIntentProbe(
        detected_policy=detected.value if detected else None,
        would_be_reason=reason.value,
        reached_corpus_branch=_freeze_product_filters(resolved_product_filters) is None,
    )


def compiled_scope_from_audit(payload: Mapping[str, object] | None) -> CompiledScope | None:
    """Rebuild a prior corpus scope from its audit JSON, or None if it is not one.

    The INHERIT branch of ``compile_scope`` needs the previous turn's audited
    corpus scope. Without it every INHERIT hint compiles to
    CORPUS_INHERITANCE_UNAUDITED and the inheritance leg is unobservable -- the
    window would report that inheritance never works, when in truth it was never
    asked.

    Strictly a reader: anything malformed, stale, or not a corpus scope returns
    None, so a corrupt audit row degrades to "no prior scope" rather than
    manufacturing an authorization out of stored text.
    """
    if not payload or str(payload.get("kind")) != CompiledScopeKind.CORPUS.value:
        return None
    try:
        policy = CorpusPolicyHint(str(payload.get("corpus_policy")))
        source = ScopeSource(str(payload.get("source")))
        reason = ScopeReason(str(payload.get("reason")))
        raw_documents = payload.get("corpus_documents")
        if not isinstance(raw_documents, list) or not raw_documents:
            return None
        documents = tuple(
            CorpusDocumentRef(
                doc_id=int(document["doc_id"]),
                version_id=int(document["version_id"]),
                appl_no=str(document["appl_no"]),
                short_name=str(document["short_name"]),
            )
            for document in raw_documents
        )
        inherited = payload.get("inherited_from_audit_id")
        return CompiledScope(
            kind=CompiledScopeKind.CORPUS,
            source=source,
            reason=reason,
            corpus_policy=policy,
            corpus_documents=tuple(sorted(documents)),
            inherited_from_audit_id=(
                int(inherited)
                if isinstance(inherited, int) and not isinstance(inherited, bool)
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def compile_scope(
    decision: RouteDecision,
    *,
    original_question: str,
    resolved_product_filters: Mapping[str, object] | None = None,
    session_product_filters: Mapping[str, object] | None = None,
    corpus_policies: Mapping[CorpusPolicyHint, CorpusPolicySnapshot] | None = None,
    prior_audited_scope: CompiledScope | None = None,
    prior_audit_id: int | None = None,
) -> CompiledScope:
    """Compile advisory output against trusted application state, failing closed."""
    resolved_product = _freeze_product_filters(resolved_product_filters)
    session_product = _freeze_product_filters(session_product_filters)

    if decision.mode is TurnMode.CONVERSE:
        return CompiledScope(
            kind=CompiledScopeKind.CONVERSE,
            source=ScopeSource.NONE,
            reason=ScopeReason.CONVERSE,
        )

    if decision.scope_hint is ScopeHint.PRODUCT:
        if resolved_product is None:
            return _clarify(ScopeReason.PRODUCT_NOT_RESOLVED)
        return _product_scope(
            resolved_product,
            source=ScopeSource.RESOLVED_PRODUCT,
            reason=ScopeReason.RESOLVED_PRODUCT,
        )

    if decision.scope_hint is ScopeHint.CORPUS:
        # A deterministic product resolution is narrower and safer than a model
        # corpus guess; this also protects the product-scoped #163 control.
        if resolved_product is not None:
            return _product_scope(
                resolved_product,
                source=ScopeSource.RESOLVED_PRODUCT,
                reason=ScopeReason.RESOLVED_PRODUCT,
            )
        detected_policy = detect_explicit_corpus_policy(original_question)
        if detected_policy is None:
            return _clarify(ScopeReason.CORPUS_INTENT_NOT_EXPLICIT)
        if detected_policy is not decision.corpus_policy_hint:
            return _clarify(ScopeReason.CORPUS_POLICY_MISMATCH)
        return _corpus_scope(
            detected_policy,
            corpus_policies=corpus_policies,
            source=ScopeSource.EXPLICIT_CORPUS,
            reason=ScopeReason.EXPLICIT_CORPUS,
        )

    if decision.scope_hint is ScopeHint.INHERIT:
        if prior_audited_scope is not None and prior_audited_scope.kind is CompiledScopeKind.CORPUS:
            if prior_audit_id is None or prior_audit_id <= 0:
                return _clarify(ScopeReason.CORPUS_INHERITANCE_UNAUDITED)
            if prior_audited_scope.corpus_policy is None:
                return _clarify(ScopeReason.CORPUS_POLICY_INVALID)
            return _corpus_scope(
                prior_audited_scope.corpus_policy,
                corpus_policies=corpus_policies,
                source=ScopeSource.AUDITED_CORPUS,
                reason=ScopeReason.INHERITED_CORPUS,
                inherited_from_audit_id=prior_audit_id,
            )
        if session_product is not None:
            return _product_scope(
                session_product,
                source=ScopeSource.SESSION_PRODUCT,
                reason=ScopeReason.SESSION_PRODUCT,
            )
        return _clarify(ScopeReason.CORPUS_INHERITANCE_UNAUDITED)

    return _clarify(ScopeReason.SCOPE_UNKNOWN)
