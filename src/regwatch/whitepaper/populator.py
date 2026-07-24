"""White-Paper populator — build the cited cell payload for an RLD + appl number.

``build_whitepaper`` resolves the spine (Drugs@FDA + Orange Book product
confirmation + DailyMed setid), then populates every registry cell by mode:

- **auto** — deterministic joins on the pinned Orange Book / NDC / Shortages /
  REMS functions. Yes/No auto cells are TRI-STATE (compliance line): a "No"
  (``verified_absent``) is emitted ONLY on a successful, identity-filtered query
  that returned zero rows; an exception/timeout/ambiguous match collapses the
  cell to ``analyst_input_required`` — a false "No" asserts an unverified fact
  (INV-5).
- **evidence_only** — verbatim cited SPL LOINC sections, label media, and the
  scoped PSG ``grounded_qa.ask()`` for Requirements. No generation.
- **manual** — always ``analyst_input_required`` with the underlying evidence
  attached (patent/exclusivity rows, REMS rows, PSG study fields). The system
  surfaces evidence; it never renders the regulatory judgment (INV-3).

Every populated cell carries provenance. A populated cell whose structured
citation is not backed by an actually-fetched row collapses to
``analyst_input_required`` (INV-8).

This module codes against the PINNED source-layer interface and never edits
``src/regwatch/sources/``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime
from typing import Any

from config.settings import get_settings
from sqlalchemy import func, or_
from sqlalchemy import select as sa_select
from sqlmodel import col, select

from regwatch.common.audit import log_query
from regwatch.common.citations import (
    is_structured_token,
    ob_token,
    obexcl_token,
    obpat_token,
    spl_token,
    validate_structured_citations,
)
from regwatch.common.logging import get_logger
from regwatch.common.text_normalize import canonical_name, names_match, stripped_name
from regwatch.generate.grounded_qa import QAResult, ask
from regwatch.generate.llm import current_model_name
from regwatch.sources import dailymed, orange_book
from regwatch.sources._utils import clean_application_number
from regwatch.sources.dailymed import SetidResolution, SplMedia, SplSection
from regwatch.sources.drugsfda import DRUGSFDA_DOC_URL, DrugsFdaHandler
from regwatch.sources.ndc import NDC_DOC_URL, NdcHandler
from regwatch.sources.orange_book import ORANGE_BOOK_SEARCH_URL
from regwatch.sources.rems import (
    REMS_INDEX_URL,
    RemsHandler,
    fetch_rems_index_html,
    parse_rems_rows,
)
from regwatch.sources.shortages import SHORTAGES_DOC_URL, ShortagesHandler
from regwatch.sources.types import SourceQuery, SourceRecord
from regwatch.store.db import session_scope
from regwatch.store.models import BeRequirement, PsgDocument
from regwatch.store.queries import current_dosage_form_routes
from regwatch.store.whitepaper_sources import (
    ObSnapshot,
    SplSnapshot,
    persist_whitepaper_snapshot,
)
from regwatch.whitepaper.template import (
    CELL_SPECS,
    CellMode,
    CellSpec,
    section_order,
    specs_for_section,
)

log = get_logger(__name__)

# SPL LOINC codes the populator fetches once (Indications, Pregnancy, Lactation,
# Females-and-males-of-reproductive-potential) — drives indication/PLLR/registry.
LOINC_INDICATION = "34067-9"
LOINC_PREGNANCY = "42228-7"
LOINC_LACTATION = "77290-5"
LOINC_REPRO = "77291-3"
_PLLR_LOINCS = (LOINC_PREGNANCY, LOINC_LACTATION, LOINC_REPRO)
_WANTED_LOINCS = (LOINC_INDICATION, *_PLLR_LOINCS)

# Canonical Physician-Labeling-Rule numbered-section LOINC codes. PLR-format
# detection is a heuristic (low confidence, analyst override) — presence of the
# numbered prescribing-information structure, never a discrete SPL field.
_PLR_SECTION_LOINCS = frozenset(
    {
        "34067-9",  # Indications and Usage
        "34068-7",  # Dosage and Administration
        "43678-2",  # Dosage Forms and Strengths
        "34070-3",  # Contraindications
        "43685-7",  # Warnings and Precautions
        "34084-4",  # Adverse Reactions
        "34073-7",  # Drug Interactions
        "43684-0",  # Use in Specific Populations
    }
)
_PLR_MIN_SECTIONS = 4

# Verbatim cited text can be long; cap the cell value so the payload stays sane.
_MAX_VALUE_CHARS = 4000

# Word markers for a pregnancy-registry mention. Toll-free numbers are matched
# by pattern (all 1-8xx prefixes), not an enumerated list — newer labels use
# 1-855/1-844/1-833, and a missed marker must never become a populated negative.
_PREGNANCY_REGISTRY_MARKERS = (
    "registry",
    "pregnancy exposure",
    "pregnancy surveillance",
)
_TOLL_FREE_RE = re.compile(r"\b1-8(?:00|33|44|55|66|77|88)\b")


class SpineResolutionError(Exception):
    """Raised when the spine cannot resolve or the RLD name and number disagree.

    Carries an explanatory ``detail`` listing what WAS found (422 on the API).
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class WhitepaperBuildTimeoutError(Exception):
    """Raised when the overall build deadline elapses.

    During the FETCH phase the build is abandoned wholesale (client-safe
    ``detail``, 504 on the API, audited by ``build_whitepaper`` with its own
    route_json reason) -- a deadline-truncated context must never populate
    cells as if its sources had actually been queried (INV-5). During the
    post-fetch cell build the same type fires at the lazy REMS index fetch,
    where the cells' existing handlers degrade it to analyst input like any
    other failed source -- the already-built paper is kept.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------------------
# Module-level fetch wrappers — direct per-handler calls so an HTTP failure is
# visible to the tri-state logic (search_sources swallows exceptions, which
# would mask a failed query as "no rows" and emit a false "No", an INV-5
# violation). Tests monkeypatch these.
# ---------------------------------------------------------------------------
def _drugsfda_records(query: SourceQuery) -> list[SourceRecord]:
    return DrugsFdaHandler().search(query)


def _ndc_records(query: SourceQuery) -> list[SourceRecord]:
    return NdcHandler().search(query)


def _shortage_records(query: SourceQuery) -> list[SourceRecord]:
    return ShortagesHandler().search(query)


def _rems_search(query: SourceQuery) -> tuple[list[SourceRecord], int]:
    """Matched REMS records plus the TOTAL parsed-row count from the index.

    The total count is the scrape-sanity signal: the REMS handler deliberately
    parses zero rows when the page shape changes, so "no rows parsed at all"
    must read as a failed query — never as "queried, genuinely absent" (INV-5).
    """
    html = fetch_rems_index_html()
    return RemsHandler(html=html).search(query), len(parse_rems_rows(html))


@dataclass
class _Ctx:
    """Everything fetched once for one populate run; extractors read from it."""

    rld_name: str
    application_number_input: str
    appl_no: str  # six-digit, no prefix
    application_type: str  # NDA | ANDA | BLA
    ingredient: str
    normalized_name: str
    now: datetime
    user_id: str | None
    product_rows: list[dict[str, str]] = field(default_factory=list)
    patent_rows: list[dict[str, str]] = field(default_factory=list)
    exclusivity_rows: list[dict[str, str]] = field(default_factory=list)
    # Per-rowset freshness: products/patents/exclusivity are fetched separately
    # and each cell/persisted row carries ITS rowset's timestamp (INV-5).
    ob_products_fetched_at: datetime | None = None
    ob_patents_fetched_at: datetime | None = None
    ob_exclusivities_fetched_at: datetime | None = None
    ob_failed: bool = False
    # True when the downloaded ZIP lacked that rowset's file: the empty rows
    # mean "file unavailable", never "queried and absent" — the cells say so
    # and persistence retains the previous durable snapshot (INV-5).
    ob_patents_member_missing: bool = False
    ob_exclusivities_member_missing: bool = False
    drugsfda_records: list[SourceRecord] = field(default_factory=list)
    setid: str | None = None
    setid_resolution: SetidResolution | None = None
    spl_sections: dict[str, SplSection] = field(default_factory=dict)
    spl_section_codes: list[str] = field(default_factory=list)
    spl_source_url: str | None = None
    spl_fetched_at: datetime | None = None
    spl_failed: bool = False
    spl_media: list[SplMedia] | None = None
    ndc_records: list[SourceRecord] | None = None  # None = the NDC query failed
    psg_docs: list[dict[str, Any]] = field(default_factory=list)
    # Same-ingredient PSGs whose dosage form/route does NOT match this
    # application — surfaced to the analyst, never cited as this form's "Yes".
    psg_other_form_docs: list[dict[str, Any]] = field(default_factory=list)
    # True when name-matched PSGs could not be form-verified because the
    # application's own form is unknown (no Orange Book product rows).
    psg_form_unverified: bool = False
    psg_store_count: int = 0
    psg_failed: bool = False
    be_requirement: dict[str, Any] | None = None
    # One REMS index fetch+parse per build (lazy); both REMS cells read it.
    rems_result: tuple[list[SourceRecord], int] | None = None
    rems_error: Exception | None = None
    known_tokens: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    # Monotonic build deadline (None = unbounded), stashed for the two live
    # calls that run AFTER the batched fetch phase: the lazy REMS index fetch
    # and the nested PSG ask() gate on what remains of it at cell-build time.
    deadline: float | None = None


# ---------------------------------------------------------------------------
# Cell builders (the wire shape).
# ---------------------------------------------------------------------------
def _evidence(
    source: str,
    locator: str,
    *,
    source_url: str | None = None,
    fetched_at: datetime | str | None = None,
    page: int | None = None,
    section: str | None = None,
    snippet: str | None = None,
) -> dict[str, Any]:
    if isinstance(fetched_at, datetime):
        fetched_at = fetched_at.isoformat()
    return {
        "source": source,
        "locator": locator,
        "source_url": source_url,
        "fetched_at": fetched_at,
        "page": page,
        "section": section,
        "snippet": snippet,
    }


def _cell(
    spec: CellSpec,
    status: str,
    value: str | None,
    evidence: list[dict[str, Any]],
    note: str | None,
) -> dict[str, Any]:
    return {
        "id": spec.id,
        "label": spec.label,
        "mode": spec.mode.value,
        "status": status,
        "value": value,
        "evidence": evidence,
        "note": note,
    }


def _populated(
    spec: CellSpec, value: str, evidence: list[dict[str, Any]], note: str | None = None
) -> dict[str, Any]:
    # Single choke point for the empty-value rule: a blank/whitespace value is
    # not a populated fact — it collapses to analyst input (INV-5), never an
    # empty cell that renders as a confident blank.
    if not value.strip():
        reason = "source returned an empty value"
        merged = f"{note.rstrip()} — {reason}." if note else f"Cell collapsed: {reason}."
        return _analyst(spec, evidence, merged)
    return _cell(spec, "populated", _clip(value), evidence, note)


def _verified_absent(
    spec: CellSpec, evidence: list[dict[str, Any]], note: str | None = None
) -> dict[str, Any]:
    # Renders as "No" — only valid on a successful, identity-filtered, empty query.
    return _cell(spec, "verified_absent", "No", evidence, note)


def _analyst(spec: CellSpec, evidence: list[dict[str, Any]], note: str | None) -> dict[str, Any]:
    return _cell(spec, "analyst_input_required", None, evidence, note)


def _clip(value: str) -> str:
    if len(value) <= _MAX_VALUE_CHARS:
        return value
    return value[:_MAX_VALUE_CHARS].rstrip() + " …[truncated]"


# ---------------------------------------------------------------------------
# Concurrent fetch stages + overall build deadline.
#
# The five fetch stages group by their real data dependencies:
#   batch A: _fetch_orange_book || _fetch_drugsfda        (identity needs both)
#   batch B: _fetch_dailymed || _fetch_ndc || _fetch_psg_store
#            (each reads only identity fields finalized before the batch)
# Everything else -- identity resolution, name reconcile, token build, persist,
# and the cell builders -- stays sequential on the caller's thread. Two live
# calls run AFTER the batched fetch phase and gate on the stashed ctx.deadline:
# the lazy REMS index fetch (bounded by the remaining time) and the nested PSG
# ask() (pre-checked only -- an in-flight LLM turn is never abandoned, it
# writes its own audit row).
# ---------------------------------------------------------------------------
def _build_deadline() -> float | None:
    """Monotonic deadline for this build; None when the bound is disabled (0)."""
    timeout_s = get_settings().whitepaper_build_timeout_s
    if timeout_s <= 0:
        return None
    return time.monotonic() + timeout_s


def _deadline_remaining(deadline: float | None) -> float | None:
    """Seconds until the build deadline; None = unbounded. May be <= 0."""
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _deadline_detail() -> str:
    timeout_s = get_settings().whitepaper_build_timeout_s
    return (
        f"White-paper build exceeded its {timeout_s:.0f}s deadline while querying FDA "
        f"sources; the build was abandoned and no partial paper was produced. Retry "
        f"shortly -- an upstream source may be slow."
    )


def _merge_stage_ctx(ctx: _Ctx, stage_ctx: _Ctx, baseline: dict[str, Any]) -> None:
    """Apply one completed stage's writes back onto the shared context.

    Deliberately field-generic: every stage ASSIGNS fresh objects over its own
    disjoint fields (verified for all five stage functions; none mutates a
    pre-batch object in place), so identity comparison finds exactly what the
    stage wrote -- a field later added to ``_Ctx`` or to a stage can never be
    silently dropped by a hand-maintained merge list. The comparison runs
    against the PRE-BATCH ``baseline``, never the live ``ctx``: a later
    stage's untouched field still holds the baseline object, and diffing it
    against a ctx already updated by an earlier stage's merge would read that
    stale value as a write and clobber the earlier result. ``warnings``
    extends in merge-call order so the payload order matches the sequential
    build's.
    """
    for f in fields(_Ctx):
        if f.name == "warnings":
            continue
        value = getattr(stage_ctx, f.name)
        if value is not baseline[f.name]:
            setattr(ctx, f.name, value)
    ctx.warnings.extend(stage_ctx.warnings)


def _run_stages_concurrently(
    ctx: _Ctx,
    stages: Sequence[Callable[[_Ctx], None]],
    deadline: float | None,
) -> None:
    """Run independent fetch stages in parallel under the overall deadline.

    Each stage runs against its own shallow copy of ``ctx`` with a private
    warnings list; completed copies merge back in SUBMISSION order, so fields
    AND warning order come out exactly as the old sequential code produced
    them no matter which thread finishes first, and worker threads never write
    shared state. Stage functions keep their own per-source try/except, so a
    failing source degrades identically to the sequential build.

    The deadline is enforced at each ``future.result(timeout=remaining)``. On
    breach the pool is abandoned WITHOUT waiting (a ``with`` block would join
    the stalled fetch): in-flight stages self-terminate via their per-call
    HTTP/DB timeouts and every fetcher context-manages a per-call client or
    session, so an abandoned thread leaks nothing. The pool is per-build and
    bounded by the stage count -- a shared pool would let one timed-out
    build's orphaned work queue starve every later build.
    """
    if deadline is not None and deadline - time.monotonic() <= 0:
        log.warning("whitepaper_build_deadline_exceeded", phase="batch_entry")
        raise WhitepaperBuildTimeoutError(_deadline_detail())
    # The pre-batch field snapshot every stage copy started from; merges diff
    # against THIS (see _merge_stage_ctx). ctx itself is only written by the
    # merges below, on this thread, after the snapshot.
    baseline = {f.name: getattr(ctx, f.name) for f in fields(_Ctx)}
    pool = ThreadPoolExecutor(max_workers=len(stages), thread_name_prefix="whitepaper-fetch")
    try:
        work: list[tuple[str, _Ctx, Future[None]]] = []
        for stage in stages:
            stage_ctx = copy.copy(ctx)
            # The shallow copy SHARES the warnings list object; give the stage
            # its own so appends stay per-stage until the ordered merge.
            stage_ctx.warnings = []
            work.append((stage.__name__, stage_ctx, pool.submit(stage, stage_ctx)))
        for stage_name, stage_ctx, future in work:
            remaining: float | None = None
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
            try:
                future.result(timeout=remaining)
            except TimeoutError as exc:
                # Unambiguously the deadline: every stage swallows its own
                # source exceptions, so a stage-raised TimeoutError never
                # reaches result() (it would already be a warning).
                log.warning(
                    "whitepaper_build_deadline_exceeded",
                    phase="stage_wait",
                    stage=stage_name,
                )
                raise WhitepaperBuildTimeoutError(_deadline_detail()) from exc
            _merge_stage_ctx(ctx, stage_ctx, baseline)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Spine resolution + context build.
# ---------------------------------------------------------------------------
_OB_TYPE_TO_APP = {"N": "NDA", "A": "ANDA", "B": "BLA"}


def _split_input(value: str) -> tuple[str, str]:
    """(six-digit appl_no, input application type) — type "" when bare digits."""
    cleaned = clean_application_number(value)
    if cleaned is None:
        raise SpineResolutionError(f"Could not parse an application number from {value!r}.")
    for prefix in ("NDA", "ANDA", "BLA"):
        if cleaned.startswith(prefix):
            return cleaned.removeprefix(prefix), prefix
    return cleaned, ""


# Containment shorter than this proves nothing ("a" is in half the formulary).
_MIN_CONTAINMENT_CHARS = 4
_MIN_RLD_NAME_CHARS = 3


def _name_matches(rld_name: str, candidates: Iterable[str]) -> bool:
    """Whether the submitted RLD name verifiably matches a candidate name.

    A 1-2 character or whitespace name cannot verify ANY application —
    bidirectional substring would wave it through — so it raises instead of
    silently matching (the spine 422s). Containment only counts when the
    contained string is >= 4 characters; exact (case-folded) equality always
    passes.
    """
    want_lc = " ".join(rld_name.lower().split())
    if len(want_lc) < _MIN_RLD_NAME_CHARS:
        raise SpineResolutionError("RLD name too short to verify against the application")
    want_canon = canonical_name(rld_name)
    want_strip = stripped_name(rld_name)
    for cand in candidates:
        cand_lc = " ".join((cand or "").lower().split())
        if not cand_lc:
            continue
        if want_lc == cand_lc:
            return True
        if want_canon and canonical_name(cand) == want_canon:
            return True
        if want_strip and stripped_name(cand) == want_strip:
            return True
        contained = want_lc if want_lc in cand_lc else cand_lc if cand_lc in want_lc else None
        if contained is not None and len(contained) >= _MIN_CONTAINMENT_CHARS:
            return True
    return False


def _build_context(rld_name: str, application_number: str, *, user_id: str | None) -> _Ctx:
    appl_no, input_type = _split_input(application_number)
    now = datetime.now(UTC)
    deadline = _build_deadline()
    ctx = _Ctx(
        rld_name=rld_name,
        application_number_input=application_number,
        appl_no=appl_no,
        application_type=input_type or "NDA",
        ingredient="",
        normalized_name="",
        now=now,
        user_id=user_id,
        deadline=deadline,
    )

    # Batch A: mutually independent; identity resolution needs both results.
    _run_stages_concurrently(ctx, (_fetch_orange_book, _fetch_drugsfda), deadline)
    _establish_identity(ctx, input_type)
    _filter_rows_to_application_type(ctx)
    _reconcile_rld_name(ctx)
    # Batch B: independent of one another; each reads only the identity fields
    # finalized above (resolved type/number, ingredient, product rows).
    _run_stages_concurrently(ctx, (_fetch_dailymed, _fetch_ndc, _fetch_psg_store), deadline)
    _build_known_tokens(ctx)
    _persist(ctx)
    return ctx


def _fetch_orange_book(ctx: _Ctx) -> None:
    try:
        products = orange_book.product_rows(ctx.application_number_input)
        patents = orange_book.patent_rows(ctx.application_number_input)
        exclusivity = orange_book.exclusivity_rows(ctx.application_number_input)
    except Exception as exc:
        ctx.ob_failed = True
        ctx.warnings.append(f"Orange Book fetch failed ({type(exc).__name__}).")
        log.warning("whitepaper_ob_fetch_failed", error=str(exc))
        return
    ctx.product_rows = products.rows
    ctx.patent_rows = patents.rows
    ctx.exclusivity_rows = exclusivity.rows
    ctx.ob_products_fetched_at = products.fetched_at
    ctx.ob_patents_fetched_at = patents.fetched_at
    ctx.ob_exclusivities_fetched_at = exclusivity.fetched_at
    ctx.ob_patents_member_missing = patents.member_missing
    ctx.ob_exclusivities_member_missing = exclusivity.member_missing
    if patents.member_missing:
        ctx.warnings.append(
            "patent.txt unavailable in this Orange Book download — patent rows could not "
            "be queried; the previous durable patent snapshot is retained."
        )
    if exclusivity.member_missing:
        ctx.warnings.append(
            "exclusivity.txt unavailable in this Orange Book download — exclusivity rows "
            "could not be queried; the previous durable exclusivity snapshot is retained."
        )


def _fetch_drugsfda(ctx: _Ctx) -> None:
    try:
        ctx.drugsfda_records = _drugsfda_records(
            SourceQuery(application_number=ctx.application_number_input, limit=5)
        )
    except Exception as exc:
        ctx.warnings.append(f"Drugs@FDA fetch failed ({type(exc).__name__}).")
        log.warning("whitepaper_drugsfda_fetch_failed", error=str(exc))


def _resolved_application_number(ctx: _Ctx) -> str:
    """The resolved, PREFIXED application number ("NDA020503").

    Post-resolution queries pass THIS — never the raw input, whose bare-digit
    candidate expansion ORs NDA/ANDA/BLA and can pull a digit-colliding
    other-type application's records into this product's cells (INV-5).
    """
    return f"{ctx.application_type}{ctx.appl_no}"


def _establish_identity(ctx: _Ctx, input_type: str) -> None:
    if not input_type:
        _reject_cross_type_ambiguity(ctx)
    ob_letter = ctx.product_rows[0].get("appl_type", "") if ctx.product_rows else ""
    ob_type = _OB_TYPE_TO_APP.get(ob_letter, "")
    if input_type:
        # An explicit prefix names ONE application; other-type rows are dropped
        # downstream, never re-typed onto the requested application.
        ctx.application_type = input_type
    elif ob_type:
        # Orange-Book-confirmed type. Drugs@FDA NEVER overrides it — its
        # bare-digit candidate expansion can return the other type's record.
        ctx.application_type = ob_type
    else:
        drugsfda_types = sorted(
            {t for t in (_record_application_type(r) for r in ctx.drugsfda_records) if t}
        )
        if len(drugsfda_types) > 1:
            found = "; ".join(f"{t} {ctx.appl_no}" for t in drugsfda_types)
            raise SpineResolutionError(
                f"Application number {ctx.appl_no} matches more than one application type in "
                f"Drugs@FDA: {found}. Re-submit with the NDA/ANDA prefix — applications are "
                f"never blended."
            )
        if drugsfda_types:
            ctx.application_type = drugsfda_types[0]
    _filter_drugsfda_to_identity(ctx)
    ingredient = ctx.product_rows[0].get("ingredient", "") if ctx.product_rows else ""
    if not ingredient:
        ingredient = _drugsfda_ingredient(ctx.drugsfda_records)
    if not ctx.product_rows and not ctx.drugsfda_records:
        # With no prefix and no source-confirmed type, ctx.application_type is
        # still the constructor default ("NDA") — don't assert it as fact.
        ident = (
            f"{ctx.application_type} {ctx.appl_no}"
            if input_type
            else f"{ctx.appl_no} (application type could not be confirmed)"
        )
        raise SpineResolutionError(
            f"No Orange Book or Drugs@FDA product found for application number "
            f"{ident}. Confirm the number is correct."
        )
    ctx.ingredient = ingredient
    ctx.normalized_name = canonical_name(ingredient) if ingredient else ""


def _filter_drugsfda_to_identity(ctx: _Ctx) -> None:
    """Keep only Drugs@FDA records that ARE the resolved application (INV-5).

    The Drugs@FDA query runs on the raw input, whose bare-digit candidate
    expansion ORs the type prefixes — so a same-digit application of another
    type can ride along and leak its sponsor/brand/forms into cells.
    """
    expected = _resolved_application_number(ctx)
    kept = [
        r
        for r in ctx.drugsfda_records
        if clean_application_number(r.identifiers.get("application_number") or "") == expected
    ]
    dropped = len(ctx.drugsfda_records) - len(kept)
    if dropped:
        ctx.warnings.append(
            f"Dropped {dropped} Drugs@FDA record(s) not matching {expected} "
            f"(applications are never blended)."
        )
    ctx.drugsfda_records = kept


def _reject_cross_type_ambiguity(ctx: _Ctx) -> None:
    """Bare-digit input matching more than one Appl_Type is ambiguous — 422.

    NDA and ANDA rows sharing the same six digits are DIFFERENT applications;
    blending them would attribute another product's strengths/flags to the
    requested one (INV-5). The failure lists what WAS found, never guesses.
    """
    by_type: dict[str, str] = {}
    for row in ctx.product_rows:
        letter = row.get("appl_type") or ""
        if letter and letter not in by_type:
            name = row.get("trade_name") or row.get("ingredient") or "unknown product"
            by_type[letter] = f"{_OB_TYPE_TO_APP.get(letter, letter)} {ctx.appl_no} ({name})"
    if len(by_type) > 1:
        found = "; ".join(by_type[letter] for letter in sorted(by_type))
        raise SpineResolutionError(
            f"Application number {ctx.appl_no} matches more than one application type in the "
            f"Orange Book: {found}. Re-submit with the NDA/ANDA prefix — applications are "
            f"never blended."
        )


def _filter_rows_to_application_type(ctx: _Ctx) -> None:
    """Drop OB rows whose Appl_Type disagrees with the resolved application type.

    ``orange_book`` keys bare-digit lookups on the number alone, so a same-digit
    application of another type can ride along in patent/exclusivity rows.
    Dropped rows are surfaced as a spine warning, never silently blended.
    """
    letter = next((k for k, v in _OB_TYPE_TO_APP.items() if v == ctx.application_type), None)
    if letter is None:
        return
    dropped = 0
    for attr in ("product_rows", "patent_rows", "exclusivity_rows"):
        rows: list[dict[str, str]] = getattr(ctx, attr)
        kept = [r for r in rows if (r.get("appl_type") or letter) == letter]
        dropped += len(rows) - len(kept)
        setattr(ctx, attr, kept)
    if dropped:
        ctx.warnings.append(
            f"Dropped {dropped} Orange Book row(s) from a different application type sharing "
            f"the digits {ctx.appl_no} (only {ctx.application_type} {ctx.appl_no} rows are used)."
        )


def _reconcile_rld_name(ctx: _Ctx) -> None:
    candidates: list[str] = []
    if ctx.ingredient:
        candidates.append(ctx.ingredient)
    for row in ctx.product_rows:
        candidates.append(row.get("trade_name", ""))
    for rec in ctx.drugsfda_records:
        for product in rec.fields.get("products") or []:
            candidates.append(str(product.get("brand_name") or ""))
        candidates.append(str(rec.fields.get("sponsor_name") or ""))
    candidates = [c for c in candidates if c]
    if not candidates:
        # Still enforce the verifiability floor — a 1-2 char name cannot
        # verify any application (raises inside _name_matches).
        _name_matches(ctx.rld_name, [])
        return
    if not _name_matches(ctx.rld_name, candidates):
        found = ", ".join(sorted({c for c in candidates})[:8])
        raise SpineResolutionError(
            f"The RLD name {ctx.rld_name!r} does not match application number "
            f"{ctx.application_type} {ctx.appl_no}. That application is associated with: "
            f"{found}. No white paper was produced — verify the RLD name and number."
        )


def _fetch_dailymed(ctx: _Ctx) -> None:
    # Prefer the sponsor's own label: DailyMed lists repackager relabels for the
    # same application number, and "most recent" alone can pick a repackager's
    # SPL (stale content, repackager cartons) over the RLD holder's current one.
    prefer = _unique(
        [
            _drugsfda_brand(ctx.drugsfda_records),
            *(row.get("trade_name") for row in ctx.product_rows),
            _drugsfda_sponsor(ctx.drugsfda_records),
        ]
    )
    # Second preference tier (P1b): sponsor/applicant names rarely appear in a
    # repackager's relabel TITLE, but DailyMed brackets the labeler
    # organization into every listing title -- matching that labeler against
    # the Drugs@FDA sponsor and Orange Book applicant names picks the RLD
    # holder's own SPL when no brand/trade title matches.
    prefer_labelers = _unique(
        [
            _drugsfda_sponsor(ctx.drugsfda_records),
            *(row.get("applicant_full_name") for row in ctx.product_rows),
            *(row.get("applicant") for row in ctx.product_rows),
        ]
    )
    try:
        # The RESOLVED, PREFIXED number — bare-digit input must not let the
        # candidate expansion resolve another type's label (contract C1).
        resolution = dailymed.resolve_setid(
            _resolved_application_number(ctx),
            prefer_titles=prefer,
            prefer_labelers=prefer_labelers,
        )
    except Exception as exc:
        ctx.warnings.append(f"DailyMed setid resolution failed ({type(exc).__name__}).")
        log.warning("whitepaper_setid_failed", error=str(exc))
        ctx.spl_failed = True
        return
    if resolution is None:
        ctx.warnings.append("DailyMed returned no SPL for this application number.")
        return
    ctx.setid_resolution = resolution
    ctx.setid = resolution.setid
    if len(resolution.candidate_labelers) > 1:
        ctx.warnings.append(
            f"DailyMed lists SPLs from {len(resolution.candidate_labelers)} distinct labelers "
            f"for this application; using {resolution.title!r} — verify it is the RLD "
            f"sponsor's label."
        )
    try:
        doc = dailymed.fetch_spl_xml(resolution.setid)
        ctx.spl_sections = dailymed.parse_spl_sections(
            doc.xml, _WANTED_LOINCS, source_url=doc.source_url, fetched_at=doc.fetched_at
        )
        ctx.spl_section_codes = dailymed.parse_spl_section_codes(doc.xml)
        ctx.spl_source_url = doc.source_url
        ctx.spl_fetched_at = doc.fetched_at
    except Exception as exc:
        ctx.spl_failed = True
        ctx.warnings.append(f"DailyMed SPL section fetch failed ({type(exc).__name__}).")
        log.warning("whitepaper_spl_sections_failed", error=str(exc))
    try:
        ctx.spl_media = dailymed.fetch_media(resolution.setid)
    except Exception as exc:
        ctx.warnings.append(f"DailyMed media fetch failed ({type(exc).__name__}).")
        log.warning("whitepaper_spl_media_failed", error=str(exc))


def _fetch_ndc(ctx: _Ctx) -> None:
    try:
        ctx.ndc_records = _ndc_records(
            SourceQuery(application_number=_resolved_application_number(ctx), limit=20)
        )
    except Exception as exc:
        ctx.ndc_records = None
        ctx.warnings.append(f"NDC Directory fetch failed ({type(exc).__name__}).")
        log.warning("whitepaper_ndc_failed", error=str(exc))


def _fetch_psg_store(ctx: _Ctx) -> None:
    try:
        with session_scope() as s:
            count = s.scalar(sa_select(func.count()).select_from(PsgDocument))
        ctx.psg_store_count = int(count or 0)
        matches = _matching_psg_docs(ctx)
        ctx.psg_docs, ctx.psg_other_form_docs = _filter_psg_by_form(ctx, matches)
        ctx.be_requirement = _latest_be_requirement(ctx)
    except Exception as exc:
        # The flag is the tri-state signal: a failed store query must never be
        # indistinguishable from a successful empty one (INV-5).
        ctx.psg_failed = True
        ctx.warnings.append(f"PSG store lookup failed ({type(exc).__name__}).")
        log.warning("whitepaper_psg_store_failed", error=str(exc))


def _build_known_tokens(ctx: _Ctx) -> None:
    tokens: set[str] = set()
    for row in ctx.product_rows:
        if row.get("product_no"):
            tokens.add(ob_token(ctx.appl_no, row["product_no"]))
    for row in ctx.patent_rows:
        if row.get("patent_no"):
            tokens.add(obpat_token(row["patent_no"]))
    for row in ctx.exclusivity_rows:
        if row.get("exclusivity_code"):
            tokens.add(obexcl_token(row["exclusivity_code"]))
    if ctx.setid:
        for loinc in ctx.spl_sections:
            tokens.add(spl_token(ctx.setid, loinc))
    ctx.known_tokens = tokens


def _drugsfda_ingredient(records: list[SourceRecord]) -> str:
    for rec in records:
        for product in rec.fields.get("products") or []:
            for ai in product.get("active_ingredients") or []:
                name = ai.get("name")
                if name:
                    return str(name)
    return ""


def _record_application_type(rec: SourceRecord) -> str | None:
    cleaned = clean_application_number(rec.identifiers.get("application_number") or "")
    if cleaned:
        for prefix in ("NDA", "ANDA", "BLA"):
            if cleaned.startswith(prefix):
                return prefix
    return None


def _matching_psg_docs(ctx: _Ctx) -> list[dict[str, Any]]:
    if not ctx.normalized_name and not ctx.appl_no:
        return []
    canon = ctx.normalized_name
    strip = stripped_name(ctx.ingredient) if ctx.ingredient else ""
    out: list[dict[str, Any]] = []
    with session_scope() as s:
        # The salt-stripped branch (stripped_name(active_ingredient) == strip) is
        # not indexable — no stored stripped column — so a non-trivial stripped
        # key still needs a full scan. Otherwise the indexed predicates
        # (normalized_name / appl_no / rld_or_rs_number) let the DB touch only
        # the matching rows instead of materializing the whole ~1,795-PSG table.
        if strip and strip != canon:
            candidates: list[PsgDocument] = list(s.scalars(select(PsgDocument)))
        else:
            preds: list[Any] = []
            if canon:
                preds.append(col(PsgDocument.normalized_name) == canon)
            if ctx.appl_no:
                preds.append(col(PsgDocument.appl_no) == ctx.appl_no)
                preds.append(col(PsgDocument.rld_or_rs_number).contains(ctx.appl_no))
            candidates = list(s.scalars(select(PsgDocument).where(or_(*preds)))) if preds else []
        for d in candidates:
            by_name = names_match(canon or "", strip, d.normalized_name, d.active_ingredient)
            by_appl = bool(d.appl_no) and d.appl_no == ctx.appl_no
            # Exact-token membership (rld_or_rs_number is a comma-joined list of
            # bare numbers) — a raw substring could span a token boundary and
            # attach another application's PSG to this product (INV-7..9).
            by_ref = bool(d.rld_or_rs_number) and ctx.appl_no in {
                t.strip().zfill(6) for t in (d.rld_or_rs_number or "").split(",") if t.strip()
            }
            if by_name or by_appl or by_ref:
                out.append(
                    {
                        "id": d.id,
                        "appl_no": d.appl_no,
                        "source_url": d.source_url,
                        "psg_type": d.psg_type,
                        "recommended_date": d.recommended_date,
                        "last_seen_at": d.last_seen_at,
                        "matched_by_appl": by_appl or by_ref,
                        "dosage_form": d.dosage_form,
                        "route": d.route,
                    }
                )
    return out


_FORM_NORM_RE = re.compile(r"[^a-z0-9]+")


def _normalized_form(value: str | None) -> str:
    return " ".join(_FORM_NORM_RE.sub(" ", (value or "").lower()).split())


def _ob_forms_and_routes(ctx: _Ctx) -> tuple[set[str], set[str]]:
    forms: set[str] = set()
    routes: set[str] = set()
    for row in ctx.product_rows:
        value = row.get("dosage_form_route", "")
        form = _normalized_form(_dosage_form_route_parts(value, 0))
        route = _normalized_form(_dosage_form_route_parts(value, 1))
        if form:
            forms.add(form)
        if route:
            routes.add(route)
    return forms, routes


def _form_compatible(value: str | None, wanted: set[str]) -> bool:
    """Whether a PSG's recorded form/route IS one of the application's own.

    Exact normalized equality only. Bidirectional substring containment let an
    immediate-release product cite the same molecule's extended-release PSG
    ("tablet" ⊂ "tablet extended release") — release types are distinct PSGs
    with materially different BE recommendations, so a containment-only match
    goes to the analyst path, never a cited "Yes" (INV-1/INV-5).
    """
    if not wanted:
        return True
    got = _normalized_form(value)
    if not got:
        # A name-only match with no recorded form is not demonstrably this
        # form's guidance — it goes to the analyst path, never a cited "Yes".
        return False
    return got in wanted


def _filter_psg_by_form(
    ctx: _Ctx, docs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(compatible docs, same-ingredient docs not verifiable as this form's).

    A PSG matched only on ingredient NAME can belong to a different dosage
    form of the same molecule — citing it as this product's BE guidance would
    attach another form's recommendation (INV-1/INV-5). Appl-no/RLD-ref
    matches are the application's own and always pass. When the application's
    own form was never established (no Orange Book product rows — ``ob_failed``
    or a Drugs@FDA-only identity), a name-only match is UNVERIFIABLE and goes
    to the analyst path, mirroring the no-recorded-form rule on the doc side.
    """
    ob_forms, ob_routes = _ob_forms_and_routes(ctx)
    kept: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    for doc in docs:
        doc["exact_form"] = bool(ob_forms) and _normalized_form(doc.get("dosage_form")) in ob_forms
        if doc.get("matched_by_appl"):
            kept.append(doc)
            continue
        if not ob_forms and not ob_routes:
            mismatched.append(doc)
            continue
        if _form_compatible(doc.get("dosage_form"), ob_forms) and _form_compatible(
            doc.get("route"), ob_routes
        ):
            kept.append(doc)
        else:
            mismatched.append(doc)
    if mismatched and not ob_forms and not ob_routes:
        ctx.psg_form_unverified = True
        ctx.warnings.append(
            f"{len(mismatched)} name-matched PSG document(s) could not be form-verified — "
            f"this application's dosage form is unknown (no Orange Book product rows); "
            f"the PSG(s) are surfaced to the analyst, never cited as this form's guidance."
        )
    return kept, mismatched


def _latest_be_requirement(ctx: _Ctx) -> dict[str, Any] | None:
    """The latest BE-requirement row for the SINGLE applicable PSG document.

    ``version_id`` orders versions WITHIN one document only — comparing it
    across documents would let an unrelated document's higher id win. The
    latest row is picked per document; with multiple surviving documents the
    exact-form match is preferred, otherwise no fields are surfaced and the
    candidate PSGs ride along as evidence (never blended).
    """
    docs = ctx.psg_docs
    doc_ids = [d["id"] for d in docs if isinstance(d.get("id"), int)]
    if not doc_ids:
        return None
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(BeRequirement).where(
                    BeRequirement.psg_document_id.in_(doc_ids)  # type: ignore[attr-defined]
                )
            )
        )
        latest_by_doc: dict[int, BeRequirement] = {}
        for row in rows:
            current = latest_by_doc.get(row.psg_document_id)
            if current is None or (row.version_id, row.id or 0) > (
                current.version_id,
                current.id or 0,
            ):
                latest_by_doc[row.psg_document_id] = row
        if not latest_by_doc:
            return None
        if len(latest_by_doc) == 1:
            chosen = next(iter(latest_by_doc.values()))
        else:
            exact_ids = {d["id"] for d in docs if d.get("exact_form")}
            exact_rows = [r for doc_id, r in latest_by_doc.items() if doc_id in exact_ids]
            if len(exact_rows) == 1:
                chosen = exact_rows[0]
            else:
                ctx.warnings.append(
                    f"{len(latest_by_doc)} distinct PSG documents carry BE requirements for "
                    f"this product and no single exact-form match exists — study fields are "
                    f"not blended; all candidate PSGs are surfaced as evidence."
                )
                return None
        url = next(
            (d["source_url"] for d in docs if d.get("id") == chosen.psg_document_id),
            None,
        )
        return {
            "fields": dict(chosen.fields_json) or _be_fields(chosen),
            "citations": dict(chosen.citations_json),
            "source_url": url,
        }


def _be_fields(row: BeRequirement) -> dict[str, Any]:
    return {
        "study_type": row.study_type,
        "study_design": row.study_design,
        "strengths": row.strengths,
        "dissolution": row.dissolution,
        "waiver_conditions": row.waiver_conditions,
        "additional_notes": row.additional_notes,
    }


# ---------------------------------------------------------------------------
# Persistence write-through (freshness provenance, INV-5). Best-effort. The
# semantics live in ``store.whitepaper_sources`` — Orange Book rows REPLACE the
# application's previous snapshot (a delisted row never lingers as stale
# evidence) and the SPL resolution upserts by setid. One implementation only.
# ---------------------------------------------------------------------------
def _persist(ctx: _Ctx) -> None:
    ob: ObSnapshot | None = None
    if not ctx.ob_failed:
        # Replace-snapshot only on a SUCCESSFUL fetch — a failed query must
        # never wipe the previous durable snapshot. A rowset whose file was
        # absent from the ZIP is that rowset's failed fetch: None skips its
        # replace so the previous durable rows survive. Each rowset carries
        # ITS fetch timestamp as freshness, never another rowset's.
        ob = ObSnapshot(
            application_number=ctx.appl_no,
            appl_type=ctx.application_type,
            product_rows=ctx.product_rows,
            patent_rows=None if ctx.ob_patents_member_missing else ctx.patent_rows,
            exclusivity_rows=(
                None if ctx.ob_exclusivities_member_missing else ctx.exclusivity_rows
            ),
            products_fetched_at=ctx.ob_products_fetched_at or ctx.now,
            patents_fetched_at=ctx.ob_patents_fetched_at or ctx.now,
            exclusivities_fetched_at=ctx.ob_exclusivities_fetched_at or ctx.now,
            source_url=ORANGE_BOOK_SEARCH_URL,
        )
    spl: SplSnapshot | None = None
    if ctx.setid_resolution is not None:
        resolution = ctx.setid_resolution
        spl = SplSnapshot(
            setid=resolution.setid,
            appl_no=ctx.appl_no,
            title=resolution.title,
            published=resolution.published,
            source_url=resolution.source_url,
            fetched_at=ctx.spl_fetched_at or resolution.fetched_at,
        )
    try:
        persist_whitepaper_snapshot(ob=ob, spl=spl)
    except Exception as exc:
        ctx.warnings.append(
            "Persistence write-through failed — the snapshot transaction rolled back "
            "atomically; no partial provenance rows were stored."
        )
        log.warning("whitepaper_persist_failed", error=str(exc))
        # Explicit Sentry capture point (H1): the populator degrades gracefully
        # (the response still ships), but losing provenance rows must be visible.
        # Sanitized: str() on the realistic failure class here (SQLAlchemy
        # StatementError/IntegrityError) embeds the failed SQL plus a parameters
        # preview — the very snapshot rows being inserted (provenance text and
        # the user-supplied RLD/application input). Forward only the exception
        # CLASS, with no cause/context chain, so none of that reaches Sentry.
        from regwatch.common.observability import capture_exception

        sanitized = RuntimeError(f"whitepaper persistence failed: {type(exc).__name__}")
        sanitized.__cause__ = None
        sanitized.__suppress_context__ = True
        capture_exception(sanitized)


# ---------------------------------------------------------------------------
# Shared evidence helpers.
# ---------------------------------------------------------------------------
def _unique(values: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    for v in values:
        cleaned = (v or "").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _product_snippet(row: dict[str, str]) -> str:
    parts = [
        row.get("ingredient", ""),
        row.get("strength", ""),
        row.get("dosage_form_route", ""),
    ]
    return " | ".join(p for p in parts if p)


def _ob_product_evidence(ctx: _Ctx, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        product_no = row.get("product_no", "")
        if not product_no:
            continue
        token = ob_token(ctx.appl_no, product_no)
        valid, _ = validate_structured_citations([token], ctx.known_tokens)
        if not valid:
            continue
        out.append(
            _evidence(
                "Orange Book",
                token,
                source_url=ORANGE_BOOK_SEARCH_URL,
                fetched_at=ctx.ob_products_fetched_at,
                snippet=_product_snippet(row),
            )
        )
    return out


def _dosage_form_route_parts(value: str, index: int) -> str:
    parts = [p.strip() for p in value.split(";")]
    if index < len(parts):
        return parts[index]
    return ""


# ---------------------------------------------------------------------------
# Extractors. Each takes (spec, ctx) and returns one cell dict.
# ---------------------------------------------------------------------------
def _ob_guard(spec: CellSpec, ctx: _Ctx) -> dict[str, Any] | None:
    if ctx.ob_failed:
        return _analyst(spec, [], "Orange Book query failed; cannot confirm this cell (INV-5).")
    if not ctx.product_rows:
        return _analyst(spec, [], "No Orange Book product rows for this application number.")
    return None


def _ext_ob_product_name(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    guard = _ob_guard(spec, ctx)
    if guard is not None:
        return guard
    ev = _ob_product_evidence(ctx, ctx.product_rows)
    if not ev:
        return _analyst(spec, [], "No validated Orange Book citation for this cell (INV-8).")
    return _populated(spec, ctx.ingredient or ctx.product_rows[0].get("ingredient", ""), ev)


def _ext_ob_dosage_form(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    guard = _ob_guard(spec, ctx)
    if guard is not None:
        return guard
    forms = _unique(
        _dosage_form_route_parts(r.get("dosage_form_route", ""), 0) for r in ctx.product_rows
    )
    ev = _ob_product_evidence(ctx, ctx.product_rows)
    if not forms or not ev:
        return _analyst(spec, ev, "Orange Book dosage form not available.")
    return _populated(spec, "; ".join(forms), ev)


def _ext_ob_route(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    guard = _ob_guard(spec, ctx)
    if guard is not None:
        return guard
    routes = _unique(
        _dosage_form_route_parts(r.get("dosage_form_route", ""), 1) for r in ctx.product_rows
    )
    ev = _ob_product_evidence(ctx, ctx.product_rows)
    if not routes or not ev:
        return _analyst(spec, ev, "Orange Book route not available.")
    return _populated(spec, "; ".join(routes), ev)


def _ext_ob_strengths(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    guard = _ob_guard(spec, ctx)
    if guard is not None:
        return guard
    strengths = _unique(r.get("strength") for r in ctx.product_rows)
    ev = _ob_product_evidence(ctx, ctx.product_rows)
    if not strengths or not ev:
        return _analyst(spec, ev, "Orange Book strength not available.")
    return _populated(spec, "; ".join(strengths), ev)


def _ext_ob_proprietary_name(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if not ctx.ob_failed and ctx.product_rows:
        names = _unique(r.get("trade_name") for r in ctx.product_rows)
        ev = _ob_product_evidence(ctx, ctx.product_rows)
        if names and ev:
            return _populated(spec, "; ".join(names), ev)
    brand = _drugsfda_brand(ctx.drugsfda_records)
    if brand:
        return _populated(spec, brand, _drugsfda_evidence(ctx))
    return _analyst(spec, [], "Proprietary name not available from Orange Book or Drugs@FDA.")


def _ext_ob_rld(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    return _ob_flag_cell(spec, ctx, "rld", "RLD")


def _ext_ob_rs(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    return _ob_flag_cell(spec, ctx, "rs", "RS")


def _ob_flag_cell(spec: CellSpec, ctx: _Ctx, field_name: str, flag: str) -> dict[str, Any]:
    guard = _ob_guard(spec, ctx)
    if guard is not None:
        return guard
    # The live products.txt carries Yes/No in the RLD/RS columns (verified
    # against the May 2026 EOBZIP); the legacy literal is accepted defensively.
    flagged = [
        r for r in ctx.product_rows if (r.get(field_name) or "").strip().upper() in ("YES", flag)
    ]
    ev = _ob_product_evidence(ctx, flagged or ctx.product_rows)
    if not ev:
        return _analyst(spec, [], "No validated Orange Book citation for this cell (INV-8).")
    if flagged:
        labels = _unique(f"product {r.get('product_no')} ({r.get('strength')})" for r in flagged)
        return _populated(spec, "Yes — " + "; ".join(labels), ev)
    return _verified_absent(
        spec, ev, f"Orange Book lists products for this application but none flagged {flag}."
    )


def _ext_nda_number(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    value = f"{ctx.application_type} {ctx.appl_no}"
    ev = _ob_product_evidence(ctx, ctx.product_rows)
    if not ev and ctx.drugsfda_records:
        ev = _drugsfda_evidence(ctx)
    if not ev:
        # A populated auto cell always carries provenance — an unconfirmed echo
        # of the input is not a sourced value (INV-5).
        return _analyst(
            spec, [], "Application number could not be confirmed against Orange Book or Drugs@FDA."
        )
    return _populated(spec, value, ev)


def _ext_nda_holder(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    sponsor = _drugsfda_sponsor(ctx.drugsfda_records)
    if sponsor:
        return _populated(spec, sponsor, _drugsfda_evidence(ctx))
    if not ctx.ob_failed and ctx.product_rows:
        holders = _unique(
            r.get("applicant_full_name") or r.get("applicant") for r in ctx.product_rows
        )
        ev = _ob_product_evidence(ctx, ctx.product_rows)
        if holders and ev:
            return _populated(spec, "; ".join(holders), ev)
    return _analyst(spec, [], "NDA holder not available from Drugs@FDA or Orange Book.")


def _drugsfda_brand(records: list[SourceRecord]) -> str:
    for rec in records:
        for product in rec.fields.get("products") or []:
            brand = product.get("brand_name")
            if brand:
                return str(brand)
    return ""


def _drugsfda_sponsor(records: list[SourceRecord]) -> str:
    for rec in records:
        sponsor = rec.fields.get("sponsor_name")
        if sponsor:
            return str(sponsor)
    return ""


def _drugsfda_evidence(ctx: _Ctx) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in ctx.drugsfda_records:
        out.append(
            _evidence(
                "Drugs@FDA (openFDA)",
                rec.identifiers.get("application_number") or "drugsfda.json",
                source_url=rec.source_url or DRUGSFDA_DOC_URL,
                fetched_at=ctx.now,
                snippet=str(rec.fields.get("sponsor_name") or rec.title),
            )
        )
    return out


def _ext_shortage(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    # Identity filter: query by the RESOLVED PREFIXED application number ONLY —
    # an ingredient search could return another product's shortage, and the raw
    # input's bare-digit expansion could return another TYPE's shortage.
    query = SourceQuery(application_number=_resolved_application_number(ctx), limit=10)
    try:
        records = _shortage_records(query)
    except Exception as exc:
        return _analyst(
            spec,
            [],
            f"Drug Shortages query failed ({type(exc).__name__}); cannot assert shortage "
            f"status (tri-state, INV-5).",
        )
    if records:
        statuses = _unique(str(r.fields.get("status") or "") for r in records)
        ev = [
            _evidence(
                "Drug Shortages (openFDA)",
                r.identifiers.get("application_number") or "shortages.json",
                source_url=r.source_url or SHORTAGES_DOC_URL,
                fetched_at=ctx.now,
                snippet=str(r.fields.get("status") or r.title),
            )
            for r in records
        ]
        # openFDA retains RESOLVED shortages as historical records — a record
        # set whose every status is Resolved must not lead with "Yes" on a
        # "currently listed?" cell (that asserts current listing from history).
        if statuses and all("resolved" in s.lower() for s in statuses):
            return _populated(
                spec,
                f"{'; '.join(statuses)} — historical shortage record(s); no current "
                f"shortage status returned.",
                ev,
                "openFDA retains resolved shortages as historical records; statuses are "
                "rendered verbatim, with no current-listing assertion.",
            )
        value = "Yes" + (f" — {'; '.join(statuses)}" if statuses else "")
        return _populated(spec, value, ev)
    return _verified_absent(
        spec,
        [
            _evidence(
                "Drug Shortages (openFDA)",
                f"shortages.json application_number={ctx.appl_no}",
                source_url=SHORTAGES_DOC_URL,
                fetched_at=ctx.now,
                snippet="Queried by application number; no shortage record returned.",
            )
        ],
        "Drug Shortages queried by application number and returned zero records.",
    )


def _rems_query(ctx: _Ctx) -> SourceQuery:
    return SourceQuery(
        application_number=_resolved_application_number(ctx),
        active_ingredient=ctx.ingredient or None,
        brand_name=_drugsfda_brand(ctx.drugsfda_records) or None,
        limit=10,
    )


def _rems_search_bounded(ctx: _Ctx) -> tuple[list[SourceRecord], int]:
    """One REMS index fetch, bounded by what remains of the build deadline.

    The index fetch is the one live call that runs OUTSIDE the batched fetch
    phase (lazy -- cell-build time), so the batch checkpoints never see it:
    unchecked, its retry budget (3 x per-call HTTP timeout) could run minutes
    past the client's bound after the fetch phase already spent the deadline.
    A breach here degrades BOTH REMS cells through their existing handlers
    (analyst input -- tri-state, INV-5); the rest of the paper is kept, unlike
    a fetch-phase breach.
    """
    query = _rems_query(ctx)
    remaining = _deadline_remaining(ctx.deadline)
    if remaining is None:
        return _rems_search(query)
    if remaining <= 0:
        log.warning("whitepaper_build_deadline_exceeded", phase="rems_entry")
        raise WhitepaperBuildTimeoutError(
            "Build deadline exceeded before the REMS index could be queried."
        )
    # Same abandon-not-join rule as _run_stages_concurrently: the worker is a
    # pure fetch+parse (no ctx, no DB session), self-terminates via its
    # per-call HTTP timeouts, and context-manages its own client, so
    # shutdown(wait=False) leaks nothing.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whitepaper-rems")
    try:
        future = pool.submit(_rems_search, query)
        try:
            return future.result(timeout=remaining)
        except TimeoutError as exc:
            if future.done():
                # The WORKER raised a TimeoutError-family error before the
                # wait expired -- a source failure, not the deadline; let the
                # normal degrade path cache and name it.
                raise
            log.warning("whitepaper_build_deadline_exceeded", phase="rems_wait")
            raise WhitepaperBuildTimeoutError(
                "Build deadline exceeded while querying the REMS index."
            ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _rems_index_results(ctx: _Ctx) -> tuple[list[SourceRecord], int]:
    """(matched records, TOTAL parsed rows) — ONE index fetch+parse per build.

    Both REMS-backed cells read this lazily: the second caller reuses the
    first's result (or re-raises its failure) instead of re-fetching the index.
    """
    if ctx.rems_error is not None:
        raise ctx.rems_error
    if ctx.rems_result is None:
        try:
            ctx.rems_result = _rems_search_bounded(ctx)
        except Exception as exc:
            ctx.rems_error = exc
            raise
    return ctx.rems_result


def _ext_rems(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    brand = _drugsfda_brand(ctx.drugsfda_records)
    if not ctx.ingredient and not brand:
        # The accessdata index is keyed by drug/program name; an appl-no-only
        # term cannot match it, so an empty result would be structural, not
        # "queried, genuinely absent" (tri-state, INV-5).
        return _analyst(
            spec,
            [],
            "No ingredient or brand name resolved — the REMS index cannot be keyed by "
            "application number alone, so absence cannot be asserted (tri-state, INV-5).",
        )
    try:
        records, total_rows = _rems_index_results(ctx)
    except Exception as exc:
        return _analyst(
            spec,
            [],
            f"REMS index query failed ({type(exc).__name__}); cannot assert REMS status "
            f"(tri-state, INV-5).",
        )
    if records:
        expected = _resolved_application_number(ctx)
        confirmed = [r for r in records if _rems_record_matches_application(r, expected)]
        ev = [
            _evidence(
                "REMS@FDA",
                r.identifiers.get("application_number") or "rems index",
                source_url=r.source_url or REMS_INDEX_URL,
                fetched_at=ctx.now,
                snippet=r.title,
            )
            for r in (confirmed or records)
        ]
        if confirmed:
            return _populated(spec, "Yes", ev, "REMS index row carries this application number.")
        # Name-only fuzzy hit: a same-ingredient REMS for ANOTHER application
        # would otherwise populate a false "Yes" — ambiguous matches collapse,
        # with the candidate rows attached as evidence (INV-5).
        return _analyst(
            spec,
            ev,
            "REMS index rows matched on ingredient/brand only (ambiguous match) — confirm "
            "whether the program applies to this application (tri-state, INV-5).",
        )
    if total_rows == 0:
        return _analyst(
            spec,
            [],
            "REMS index returned no parseable rows (the page shape may have changed); cannot "
            "assert REMS absence (tri-state, INV-5).",
        )
    return _verified_absent(
        spec,
        [
            _evidence(
                "REMS@FDA",
                f"rems index application_number={ctx.appl_no}",
                source_url=REMS_INDEX_URL,
                fetched_at=ctx.now,
                snippet=f"Index parsed ({total_rows} rows); no matching REMS program returned.",
            )
        ],
        f"REMS index parsed ({total_rows} rows) and returned zero matches for this product.",
    )


# How the index embeds application numbers in row free text: "NDA #022549".
_REMS_APP_NO_TEXT_RE = re.compile(r"\b(?:NDA|ANDA|BLA)\s*#?\s*\d{5,6}\b", re.IGNORECASE)


def _rems_record_matches_application(record: SourceRecord, expected: str) -> bool:
    """True only when a TYPED application number on the row IS this application.

    ``expected`` is the resolved, prefixed number ("NDA020503"). Candidate
    numbers come only from application-number-bearing fields — the structured
    identifiers, the raw application-number column, and explicit
    "NDA #022549"-style free-text mentions — each cleaned and compared for
    EXACT equality. A bare-digit value never confirms (it cannot name a type),
    and arbitrary raw values (URLs, dates) are never substring-scanned: digits
    embedded in an unrelated value must not assert a REMS program (INV-5).
    """
    candidates: list[str] = []
    for key in ("application_number", "application_numbers"):
        value = record.identifiers.get(key)
        if value:
            candidates.extend(part for part in str(value).split(",") if part.strip())
    for key in ("application_number", "application_no"):
        raw_value = record.raw.get(key)
        if raw_value:
            candidates.append(str(raw_value))
    for raw_value in record.raw.values():
        candidates.extend(m.group(0) for m in _REMS_APP_NO_TEXT_RE.finditer(str(raw_value)))
    return any(clean_application_number(c) == expected for c in candidates)


def _ext_packaging(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if ctx.ndc_records is None:
        return _analyst(spec, [], "NDC Directory query failed; packaging not available.")
    package_ndcs: list[str] = []
    ev: list[dict[str, Any]] = []
    for rec in ctx.ndc_records:
        for pack in rec.fields.get("packaging") or []:
            pkg = str(pack.get("package_ndc") or "")
            if pkg:
                package_ndcs.append(pkg)
        ev.append(
            _evidence(
                "NDC Directory (openFDA)",
                rec.identifiers.get("product_ndc") or "ndc.json",
                source_url=rec.source_url or NDC_DOC_URL,
                fetched_at=ctx.now,
                snippet="; ".join(
                    str(p.get("description") or p.get("package_ndc") or "")
                    for p in (rec.fields.get("packaging") or [])
                )
                or None,
            )
        )
    package_ndcs = _unique(package_ndcs)
    if not package_ndcs:
        return _analyst(
            spec, ev, "NDC query returned no packaging entries for this application number."
        )
    return _populated(spec, "; ".join(package_ndcs), ev)


def _ext_epc(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if ctx.ndc_records is None:
        return _analyst(spec, [], "openFDA query failed; EPC not available.")
    classes: list[str] = []
    ev: list[dict[str, Any]] = []
    for rec in ctx.ndc_records:
        for cls in rec.raw.get("pharm_class") or []:
            if "[EPC]" in str(cls):
                classes.append(str(cls))
        if rec.raw.get("pharm_class"):
            ev.append(
                _evidence(
                    "openFDA NDC pharm_class",
                    rec.identifiers.get("product_ndc") or "ndc.json",
                    source_url=rec.source_url or NDC_DOC_URL,
                    fetched_at=ctx.now,
                    snippet="; ".join(str(c) for c in rec.raw.get("pharm_class") or []),
                )
            )
    classes = _unique(classes)
    if not classes:
        return _analyst(
            spec,
            ev,
            "No Established Pharmacologic Class ([EPC]) found in the NDC pharm_class field.",
        )
    return _populated(spec, "; ".join(classes), ev)


def _ext_dea(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if ctx.ndc_records is None:
        return _analyst(spec, [], "openFDA query failed; DEA schedule not available.")
    schedules: list[str] = []
    ev: list[dict[str, Any]] = []
    for rec in ctx.ndc_records:
        sched = rec.raw.get("dea_schedule")
        if sched:
            schedules.append(str(sched))
            ev.append(
                _evidence(
                    "openFDA NDC dea_schedule",
                    rec.identifiers.get("product_ndc") or "ndc.json",
                    source_url=rec.source_url or NDC_DOC_URL,
                    fetched_at=ctx.now,
                    snippet=str(sched),
                )
            )
    schedules = _unique(schedules)
    if not schedules:
        # Absence of the field does NOT prove non-scheduled — collapse, don't
        # assert "N/A" (INV-5).
        return _analyst(
            spec,
            [],
            "No dea_schedule field in the NDC directory record — confirm controlled-substance "
            "status with the analyst (absence does not prove N/A).",
        )
    return _populated(spec, "; ".join(schedules), ev)


def _spl_guard(spec: CellSpec, ctx: _Ctx) -> dict[str, Any] | None:
    if ctx.setid is None:
        return _analyst(spec, [], "Could not resolve a DailyMed SPL for this application number.")
    if ctx.spl_failed:
        return _analyst(spec, [], "DailyMed SPL fetch failed; labeling cell not available.")
    return None


def _ext_spl_section(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    guard = _spl_guard(spec, ctx)
    if guard is not None:
        return guard
    loinc = spec.arg or LOINC_INDICATION
    section = ctx.spl_sections.get(loinc)
    if section is None or not section.text:
        return _analyst(spec, [], f"SPL has no LOINC {loinc} section text.")
    assert ctx.setid is not None  # noqa: S101 - narrowing; _spl_guard already gated setid
    token = spl_token(ctx.setid, loinc)
    valid, _ = validate_structured_citations([token], ctx.known_tokens)
    if not valid:
        return _analyst(spec, [], "SPL section citation failed validation (INV-8).")
    ev = [
        _evidence(
            "DailyMed SPL",
            token,
            source_url=section.source_url,
            fetched_at=section.fetched_at,
            section=section.title or loinc,
            snippet=section.text[:600],
        )
    ]
    return _populated(spec, section.text, ev)


def _ext_spl_media(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if ctx.setid is None:
        return _analyst(spec, [], "Could not resolve a DailyMed SPL; labeling images unavailable.")
    if ctx.spl_media is None:
        return _analyst(spec, [], "DailyMed media manifest fetch failed.")
    if not ctx.spl_media:
        return _populated(
            spec,
            "0 labeling images in the SPL media manifest.",
            [
                _evidence(
                    "DailyMed media.json",
                    f"setid={ctx.setid}",
                    source_url=dailymed.SPL_MEDIA_URL_TEMPLATE.format(setid=ctx.setid),
                    fetched_at=ctx.now,
                    snippet="No media assets enumerated.",
                )
            ],
        )
    ev = [
        _evidence(
            "DailyMed media.json",
            media.name,
            source_url=media.url,
            fetched_at=ctx.now,
            snippet=media.mime_type,
        )
        for media in ctx.spl_media
    ]
    return _populated(spec, f"{len(ctx.spl_media)} labeling image(s) enumerated.", ev)


def _structure_guard(spec: CellSpec, ctx: _Ctx) -> dict[str, Any] | None:
    """Collapse PLR/PLLR structure cells when the section parse degraded.

    Every conformant SPL carries LOINC-coded sections, so an empty parsed code
    list signals namespace/structure drift, not a label without sections — a
    confident structural negative from that state is unverified (INV-5).
    """
    guard = _spl_guard(spec, ctx)
    if guard is not None:
        return guard
    if not ctx.spl_section_codes:
        return _analyst(
            spec,
            [],
            "SPL fetched but no LOINC-coded sections parsed — cannot assess labeling "
            "structure (INV-5).",
        )
    return None


def _ext_plr_format(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    guard = _structure_guard(spec, ctx)
    if guard is not None:
        return guard
    present = [c for c in ctx.spl_section_codes if c in _PLR_SECTION_LOINCS]
    ev = [
        _evidence(
            "DailyMed SPL structure",
            f"setid={ctx.setid}",
            source_url=ctx.spl_source_url,
            fetched_at=ctx.spl_fetched_at,
            snippet="numbered PLR sections present: " + (", ".join(present) or "none"),
        )
    ]
    note = (
        "Low-confidence structural heuristic (Highlights + numbered prescribing-information "
        "sections) — analyst override expected."
    )
    if len(present) >= _PLR_MIN_SECTIONS:
        return _populated(spec, "PLR format (numbered PI sections detected)", ev, note)
    return _populated(
        spec,
        "Likely pre-PLR / non-standard format (numbered PLR sections not detected)",
        ev,
        note,
    )


def _ext_pllr_format(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    guard = _structure_guard(spec, ctx)
    if guard is not None:
        return guard
    present = [c for c in _PLLR_LOINCS if c in ctx.spl_section_codes]
    assert ctx.setid is not None  # noqa: S101 - narrowing; _spl_guard already gated setid
    ev: list[dict[str, Any]] = []
    for loinc in present:
        token = spl_token(ctx.setid, loinc)
        valid, _ = validate_structured_citations([token], ctx.known_tokens)
        ev.append(
            _evidence(
                "DailyMed SPL",
                token if valid else f"setid={ctx.setid}#{loinc}",
                source_url=ctx.spl_source_url,
                fetched_at=ctx.spl_fetched_at,
                section=loinc,
                snippet="PLLR subsection present",
            )
        )
    if present:
        return _populated(spec, f"PLLR subsections present: {', '.join(present)}", ev)
    return _populated(
        spec,
        "No PLLR subsections (LOINC 42228-7 / 77290-5 / 77291-3) detected.",
        [
            _evidence(
                "DailyMed SPL structure",
                f"setid={ctx.setid}",
                source_url=ctx.spl_source_url,
                fetched_at=ctx.spl_fetched_at,
                snippet="PLLR subsection codes absent from the section list.",
            )
        ],
        "Structural check on the parsed SPL section-code list — analyst override expected.",
    )


def _ext_pregnancy_registry(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    guard = _spl_guard(spec, ctx)
    if guard is not None:
        return guard
    section = ctx.spl_sections.get(LOINC_PREGNANCY)
    if section is None:
        return _analyst(spec, [], "No Pregnancy subsection (LOINC 42228-7) in the SPL.")
    assert ctx.setid is not None  # noqa: S101 - narrowing; _spl_guard already gated setid
    token = spl_token(ctx.setid, LOINC_PREGNANCY)
    valid, _ = validate_structured_citations([token], ctx.known_tokens)
    locator = token if valid else f"setid={ctx.setid}#{LOINC_PREGNANCY}"
    match = _find_registry_sentence(section.text)
    if match:
        ev = [
            _evidence(
                "DailyMed SPL",
                locator,
                source_url=section.source_url,
                fetched_at=section.fetched_at,
                section=section.title or LOINC_PREGNANCY,
                snippet=match,
            )
        ]
        return _populated(spec, match, ev)
    # No marker matched: the scan is recall-limited, so a generated "no registry"
    # sentence would be an asserted negative in an evidence-only cell (INV-5) —
    # surface the subsection and hand the call to the analyst.
    return _analyst(
        spec,
        [
            _evidence(
                "DailyMed SPL",
                locator,
                source_url=section.source_url,
                fetched_at=section.fetched_at,
                section=section.title or LOINC_PREGNANCY,
                snippet=section.text[:300],
            )
        ],
        "No registry marker (registry / pregnancy exposure / toll-free contact) detected in "
        "the Pregnancy subsection — analyst confirmation required (the scan is "
        "recall-limited).",
    )


def _registry_marker_in(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in _PREGNANCY_REGISTRY_MARKERS):
        return True
    return bool(_TOLL_FREE_RE.search(lowered))


def _find_registry_sentence(text: str) -> str | None:
    if not _registry_marker_in(text):
        return None
    for sentence in _split_sentences(text):
        if _registry_marker_in(sentence):
            return sentence.strip()
    return None


def _split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _ext_be_guidance_available(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if ctx.psg_failed:
        # A failed store query left psg_docs empty — that emptiness is not a
        # successful empty result and must never render "No" (tri-state, INV-5).
        return _analyst(
            spec,
            [],
            "PSG store lookup failed; cannot assert guidance absence (tri-state, INV-5).",
        )
    docs = ctx.psg_docs
    if docs:
        ev = [
            _evidence(
                "PSG store",
                f"PSG_{d.get('appl_no') or ctx.appl_no}",
                source_url=d.get("source_url"),
                fetched_at=d.get("last_seen_at"),
                snippet=f"{d.get('psg_type')} PSG (recommended {d.get('recommended_date') or 'n/a'})",
            )
            for d in docs
        ]
        return _populated(spec, "Yes", ev)
    if ctx.psg_other_form_docs:
        # Same-ingredient PSGs exist, but for OTHER dosage forms (or for forms
        # that cannot be verified against this application) — citing one as
        # this form's guidance would attach another form's recommendation,
        # and "No" would deny guidance the analyst should review (INV-1/5).
        ev = [
            _evidence(
                "PSG store",
                f"PSG_{d.get('appl_no') or ctx.appl_no}",
                source_url=d.get("source_url"),
                fetched_at=d.get("last_seen_at"),
                snippet=f"{d.get('psg_type')} PSG ({d.get('dosage_form') or 'unspecified form'})",
            )
            for d in ctx.psg_other_form_docs
        ]
        if ctx.psg_form_unverified:
            return _analyst(
                spec,
                ev,
                "PSG(s) matched this ingredient by name, but this application's dosage "
                "form could not be established (no Orange Book product rows) — the "
                "match cannot be verified as this form's guidance, so neither 'Yes' "
                "nor 'No' can be asserted (INV-1/5).",
            )
        forms = _unique(
            f"{d.get('dosage_form') or 'unspecified form'}"
            f" / {d.get('route') or 'unspecified route'}"
            for d in ctx.psg_other_form_docs
        )
        return _analyst(
            spec,
            ev,
            f"PSG(s) found for this ingredient only in other dosage form(s): "
            f"{'; '.join(forms)} — none matches this application's form, so neither "
            f"'Yes' nor 'No' can be asserted (forms are never blended, INV-1/5).",
        )
    if ctx.psg_store_count <= 0:
        # An empty local store proves nothing about FDA's catalog — "queried,
        # genuinely absent" requires a corpus to be absent FROM (INV-5).
        return _analyst(
            spec,
            [],
            "PSG store is empty/unseeded — absence cannot be verified (tri-state, INV-5).",
        )
    return _verified_absent(
        spec,
        [
            _evidence(
                "PSG store",
                f"appl_no={ctx.appl_no}",
                fetched_at=ctx.now,
                snippet=f"No PSG among {ctx.psg_store_count} stored document(s) keyed to "
                f"this application number/ingredient.",
            )
        ],
        "Local PSG store queried; no product-specific guidance present.",
    )


def _guiding_note(qa: QAResult, base: str) -> str:
    """Turn a dead-end collapse string into a guiding note for the analyst.

    Pure string assembly from fields ALREADY on the returned ``QAResult`` (no new
    model call, no retrieval, no fabricated content). The cell still collapses to
    ``analyst_input_required`` with ``value=None`` (INV-5) — this only enriches the
    existing ``note`` so the analyst sees the closest match + concrete next steps.
    Every field is None-safe: the refused path leaves ``interpretation`` None and
    ``related``/``clarify`` empty, so the note degrades to the base string alone.
    """
    note = base
    if qa.interpretation:
        note = f"{note} Closest matching guidance: {qa.interpretation}"
    labels = [o.label for o in (qa.related or [])] + [o.label for o in (qa.clarify or [])]
    if labels:
        note = f'{note} Answerable next steps: {"; ".join(labels[:3])}.'
    return note


def _psg_ask_form_filters(ctx: _Ctx) -> dict[str, str] | None:
    """EXACT stored (dosage_form, route) strings scoping the Requirements ask.

    A multi-form ingredient (albuterol spans four dosage forms in the PSG
    corpus) collapses a name-only ask to a multi_form clarify even when THIS
    application is single-form. The retriever's form/route filter matches the
    STORED values, so the scope must be a pair the store literally carries: a
    manufactured value would silently zero retrieval and turn a real product
    into a wrong refusal (INV-5). Filters are returned only when BOTH hold:
    the application's own Orange Book identity has exactly one distinct
    normalized (form, route), and exactly one stored vocabulary pair
    normalizes to it. Anything else -- multi-form application, no or ambiguous
    stored pair, vocabulary lookup failure -- returns None and the ask stays
    unfiltered (today's clarify/guiding-note behavior; forms are never
    blended, INV-1).
    """
    ob_forms, ob_routes = _ob_forms_and_routes(ctx)
    if len(ob_forms) != 1 or len(ob_routes) != 1:
        return None
    ob_form = next(iter(ob_forms))
    ob_route = next(iter(ob_routes))
    try:
        combos = current_dosage_form_routes(ctx.normalized_name)
    except Exception as exc:
        log.warning("whitepaper_psg_form_vocab_failed", error=str(exc))
        return None
    matches = [
        (form, route)
        for form, route in combos
        if _normalized_form(form) == ob_form and _normalized_form(route) == ob_route
    ]
    if len(matches) != 1:
        return None
    form, route = matches[0]
    return {"dosage_form": form, "route": route}


def _ext_psg_requirements(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if not ctx.normalized_name:
        return _analyst(spec, [], "No normalized ingredient resolved; cannot scope the PSG ask.")
    remaining = _deadline_remaining(ctx.deadline)
    if remaining is not None and remaining <= 0:
        # The nested ask() (retrieval + LLM synthesis + its own audit row) is
        # the other post-fetch live call the batch checkpoints never see; it
        # must not START past the build deadline. Entry gate only -- an
        # in-flight ask is never abandoned -- and skipping degrades exactly
        # like a failed ask: analyst input, never a guessed answer.
        log.warning("whitepaper_build_deadline_exceeded", phase="psg_ask_entry")
        return _analyst(
            spec,
            [],
            "Build deadline exceeded before the scoped PSG ask could run; the PSG corpus "
            "was not queried (tri-state, INV-5).",
        )
    question = (
        f"What are the recommended bioequivalence study design and acceptance criteria for "
        f"{ctx.ingredient} generic products?"
    )
    filters: dict[str, Any] = {"normalized_name": ctx.normalized_name}
    form_filters = _psg_ask_form_filters(ctx)
    if form_filters is not None:
        filters.update(form_filters)
    try:
        qa = ask(
            question,
            filters=filters,
            user_id=ctx.user_id,
            bind_session=False,
        )
    except Exception as exc:
        return _analyst(spec, [], f"Scoped PSG ask failed ({type(exc).__name__}).")
    if qa.status == "clarify":
        return _analyst(
            spec,
            [],
            _guiding_note(
                qa,
                "PSG corpus spans more than one dosage form for this ingredient; analyst must "
                "select the form (forms are not blended, INV-1).",
            ),
        )
    if qa.refused or qa.status == "refused":
        return _analyst(
            spec,
            [],
            _guiding_note(
                qa,
                "Scoped PSG ask refused — the ingredient is not in the corpus or retrieval was "
                "below threshold (INV-9).",
            ),
        )
    ev = [
        _evidence(
            "PSG (grounded Q&A)",
            f"{c.short_name}, p.{c.page}",
            source_url=c.source_url,
            page=c.page,
            fetched_at=ctx.now,
            snippet=c.snippet,
        )
        for c in qa.citations
    ]
    if not ev:
        return _analyst(
            spec,
            [],
            _guiding_note(qa, "Scoped PSG ask produced no validated citations (INV-1)."),
        )
    return _populated(spec, qa.answer, ev)


# ---- manual extractors (always analyst_input_required; surface evidence) ----
def _ext_manual_no_source(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    return _analyst(
        spec,
        [],
        f"No machine-readable FDA source ({spec.source}). Analyst input required.",
    )


def _ext_patent_block(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if ctx.ob_failed:
        return _analyst(spec, [], "Orange Book patent query failed; analyst input required.")
    ev: list[dict[str, Any]] = []
    for row in ctx.patent_rows:
        patent_no = row.get("patent_no", "")
        if not patent_no:
            continue
        token = obpat_token(patent_no)
        valid, _ = validate_structured_citations([token], ctx.known_tokens)
        if not valid:
            continue
        ev.append(
            _evidence(
                "Orange Book patent.txt",
                token,
                source_url=ORANGE_BOOK_SEARCH_URL,
                fetched_at=ctx.ob_patents_fetched_at,
                snippet=_patent_snippet(row),
            )
        )
    if ev:
        note = (
            "Orange Book patent rows surfaced as evidence; paragraph classification / "
            "priority posture is regulatory judgment (INV-3)."
        )
    elif ctx.ob_patents_member_missing:
        # The file was absent from the download — "no rows for this
        # application" would be a queried-and-absent claim the data cannot
        # support (INV-5).
        note = (
            "patent.txt unavailable in this Orange Book download — patent rows could not "
            "be queried; patent posture is analyst judgment (INV-3)."
        )
    else:
        note = (
            "No Orange Book patent rows for this application; patent posture is analyst "
            "judgment (INV-3)."
        )
    return _analyst(spec, ev, note)


def _patent_snippet(row: dict[str, str]) -> str:
    parts = [
        f"patent {row.get('patent_no')}",
        f"expires {row.get('patent_expire_date')}" if row.get("patent_expire_date") else "",
        f"DS={row.get('drug_substance_flag')}" if row.get("drug_substance_flag") else "",
        f"DP={row.get('drug_product_flag')}" if row.get("drug_product_flag") else "",
        f"use code {row.get('patent_use_code')}" if row.get("patent_use_code") else "",
    ]
    return " | ".join(p for p in parts if p)


def _ext_exclusivity_block(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if ctx.ob_failed:
        return _analyst(spec, [], "Orange Book exclusivity query failed; analyst input required.")
    ev: list[dict[str, Any]] = []
    for row in ctx.exclusivity_rows:
        code = row.get("exclusivity_code", "")
        if not code:
            continue
        token = obexcl_token(code)
        valid, _ = validate_structured_citations([token], ctx.known_tokens)
        if not valid:
            continue
        ev.append(
            _evidence(
                "Orange Book exclusivity.txt",
                token,
                source_url=ORANGE_BOOK_SEARCH_URL,
                fetched_at=ctx.ob_exclusivities_fetched_at,
                snippet=f"code {code} (expires {row.get('exclusivity_date') or 'n/a'})",
            )
        )
    if ev:
        note = (
            "Orange Book exclusivity rows surfaced as evidence; First-to-Market / eFTF "
            "eligibility is regulatory judgment (INV-3)."
        )
    elif ctx.ob_exclusivities_member_missing:
        note = (
            "exclusivity.txt unavailable in this Orange Book download — exclusivity rows "
            "could not be queried; eligibility is analyst judgment (INV-3)."
        )
    else:
        note = (
            "No Orange Book exclusivity rows for this application; eligibility is analyst "
            "judgment (INV-3)."
        )
    return _analyst(spec, ev, note)


def _ext_priority_block(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    """Patent AND exclusivity rows together — the schema's Priority Status evidence."""
    patents = _ext_patent_block(spec, ctx)
    exclusivity = _ext_exclusivity_block(spec, ctx)
    ev = [*patents["evidence"], *exclusivity["evidence"]]
    note = (
        "Orange Book patent + exclusivity rows surfaced as evidence; the priority posture "
        "is regulatory judgment (INV-3)."
    )
    return _analyst(spec, ev, note)


def _ext_combination_product(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    ev: list[dict[str, Any]] = []
    for rec in ctx.drugsfda_records:
        forms = _unique(
            f"{p.get('dosage_form')} / {p.get('route')}" for p in (rec.fields.get("products") or [])
        )
        ev.append(
            _evidence(
                "Drugs@FDA (openFDA)",
                rec.identifiers.get("application_number") or "drugsfda.json",
                source_url=rec.source_url or DRUGSFDA_DOC_URL,
                fetched_at=ctx.now,
                snippet="; ".join(forms) or rec.title,
            )
        )
    note = (
        "Combination-product Type 1-9 is a 21 CFR 3.2(e) determination - NOT the Orange Book "
        "'type' column (marketing status). Dosage form / delivery system surfaced as evidence "
        "for the analyst (INV-3)."
    )
    return _analyst(spec, ev, note)


def _ext_restricted_distribution(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    # Same identity terms (ingredient + brand) and the same single per-build
    # REMS index fetch as the REMS Y/N cell — including its parse-sanity rule.
    ev: list[dict[str, Any]] = []
    note_tail = ""
    brand = _drugsfda_brand(ctx.drugsfda_records)
    if not ctx.ingredient and not brand:
        note_tail = (
            " No ingredient or brand name resolved — the REMS index cannot be keyed by "
            "application number alone."
        )
    else:
        try:
            records, total_rows = _rems_index_results(ctx)
        except Exception as exc:
            note_tail = f" REMS query failed ({type(exc).__name__})."
        else:
            if total_rows == 0:
                note_tail = (
                    " REMS index returned no parseable rows (the page shape may have "
                    "changed); REMS evidence unavailable."
                )
            else:
                ev = [
                    _evidence(
                        "REMS@FDA",
                        rec.identifiers.get("application_number") or "rems index",
                        source_url=rec.source_url or REMS_INDEX_URL,
                        fetched_at=ctx.now,
                        snippet=rec.title,
                    )
                    for rec in records
                ]
    note = (
        "Restricted-distribution / ETASU is an interpretation of the REMS program — analyst "
        "judgment (INV-3); REMS rows surfaced as evidence." + note_tail
    )
    return _analyst(spec, ev, note)


def _ext_labeling_carveouts(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    ev: list[dict[str, Any]] = []
    if not ctx.ob_failed:
        for row in ctx.patent_rows:
            use_code = row.get("patent_use_code")
            patent_no = row.get("patent_no", "")
            if not use_code or not patent_no:
                continue
            token = obpat_token(patent_no)
            valid, _ = validate_structured_citations([token], ctx.known_tokens)
            if not valid:
                continue
            ev.append(
                _evidence(
                    "Orange Book patent.txt",
                    token,
                    source_url=ORANGE_BOOK_SEARCH_URL,
                    fetched_at=ctx.ob_patents_fetched_at,
                    snippet=f"patent {patent_no} use code {use_code}",
                )
            )
    indication = ctx.spl_sections.get(LOINC_INDICATION)
    if indication is not None and ctx.setid is not None:
        token = spl_token(ctx.setid, LOINC_INDICATION)
        valid, _ = validate_structured_citations([token], ctx.known_tokens)
        if valid:
            ev.append(
                _evidence(
                    "DailyMed SPL",
                    token,
                    source_url=indication.source_url,
                    fetched_at=indication.fetched_at,
                    section=indication.title or LOINC_INDICATION,
                    snippet=indication.text[:300],
                )
            )
    note = (
        "Carve-out decision is regulatory judgment (INV-3): protected-use patent codes and the "
        "RLD indication are surfaced as evidence for the analyst."
    )
    return _analyst(spec, ev, note)


def _ext_psg_strategy(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    ev = _be_requirement_evidence(ctx, None)
    note = (
        "A proposed BE strategy is a recommendation — analyst judgment (scope_warning, INV-3). "
        "PSG study fields surfaced as evidence."
    )
    return _analyst(spec, ev, note)


def _ext_be_requirement_field(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    ev = _be_requirement_evidence(ctx, spec.arg)
    # The note states what evidence actually EXISTS for this study cell so the
    # analyst is guided, not dead-ended -- the cell itself stays analyst input
    # (INV-3) and nothing is asserted beyond what was extracted (INV-5).
    base = "Per-study requirement is analyst judgment (INV-3)"
    be_fields = (ctx.be_requirement or {}).get("fields") or {}
    docs_tail = (
        "the PSG document(s) are surfaced as evidence"
        if ev
        else "no PSG evidence is available for this product"
    )
    if spec.arg and be_fields.get(spec.arg):
        # Claim a page citation only when one was actually extracted (INV-5:
        # the note may not assert evidence the cell does not carry).
        cite = ((ctx.be_requirement or {}).get("citations") or {}).get(spec.arg)
        has_page = isinstance(cite, dict) and isinstance(cite.get("page"), int)
        cited = "text and its page citation are" if has_page else "text is"
        note = f"{base}; the PSG's extracted {spec.arg!r} {cited} surfaced as evidence."
    elif spec.arg:
        note = f"{base}; no {spec.arg!r} text was extracted from this product's PSG - {docs_tail}."
    else:
        note = f"{base}; no machine-extracted PSG field maps to this study cell - {docs_tail}."
    return _analyst(spec, ev, note)


def _be_requirement_evidence(ctx: _Ctx, field_name: str | None) -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = []
    be = ctx.be_requirement
    if be is not None and field_name:
        value = (be.get("fields") or {}).get(field_name)
        if value:
            cite = (be.get("citations") or {}).get(field_name) or {}
            page = cite.get("page") if isinstance(cite, dict) else None
            ev.append(
                _evidence(
                    "PSG (BE requirement)",
                    f"{field_name}",
                    source_url=be.get("source_url"),
                    page=page if isinstance(page, int) else None,
                    fetched_at=ctx.now,
                    snippet=str(value),
                )
            )
    for doc in ctx.psg_docs:
        ev.append(
            _evidence(
                "PSG store",
                f"PSG_{doc.get('appl_no') or ctx.appl_no}",
                source_url=doc.get("source_url"),
                fetched_at=doc.get("last_seen_at"),
                snippet=f"{doc.get('psg_type')} PSG",
            )
        )
    return ev


EXTRACTORS: dict[str, Callable[[CellSpec, _Ctx], dict[str, Any]]] = {
    "ob_product_name": _ext_ob_product_name,
    "ob_dosage_form": _ext_ob_dosage_form,
    "ob_route": _ext_ob_route,
    "ob_strengths": _ext_ob_strengths,
    "ob_proprietary_name": _ext_ob_proprietary_name,
    "ob_rld": _ext_ob_rld,
    "ob_rs": _ext_ob_rs,
    "nda_number": _ext_nda_number,
    "nda_holder": _ext_nda_holder,
    "shortage": _ext_shortage,
    "rems": _ext_rems,
    "packaging": _ext_packaging,
    "epc": _ext_epc,
    "dea": _ext_dea,
    "spl_section": _ext_spl_section,
    "spl_media": _ext_spl_media,
    "plr_format": _ext_plr_format,
    "pllr_format": _ext_pllr_format,
    "pregnancy_registry": _ext_pregnancy_registry,
    "be_guidance_available": _ext_be_guidance_available,
    "psg_requirements": _ext_psg_requirements,
    "manual_no_source": _ext_manual_no_source,
    "patent_block": _ext_patent_block,
    "exclusivity_block": _ext_exclusivity_block,
    "priority_block": _ext_priority_block,
    "combination_product": _ext_combination_product,
    "restricted_distribution": _ext_restricted_distribution,
    "labeling_carveouts": _ext_labeling_carveouts,
    "psg_strategy": _ext_psg_strategy,
    "be_requirement_field": _ext_be_requirement_field,
}


# ---------------------------------------------------------------------------
# INV-8 central guard + section assembly.
# ---------------------------------------------------------------------------
def _enforce_structured_citations(cell: dict[str, Any], known: set[str]) -> dict[str, Any]:
    """Collapse a populated cell whose structured citation is not backed (INV-8)."""
    if cell["status"] != "populated":
        return cell
    for ev in cell["evidence"]:
        locator = ev.get("locator") or ""
        if is_structured_token(locator):
            valid, _ = validate_structured_citations([locator], known)
            if not valid:
                note = (cell.get("note") or "").rstrip()
                suffix = (
                    "Structured citation failed validation — collapsed to analyst input (INV-8)."
                )
                cell = {
                    **cell,
                    "status": "analyst_input_required",
                    "value": None,
                    "note": (f"{note} {suffix}").strip(),
                }
                break
    return cell


def _build_cell(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    extractor = EXTRACTORS.get(spec.extractor)
    if extractor is None:  # pragma: no cover - registry/extractor drift guard
        return _analyst(spec, [], f"No extractor registered for {spec.extractor!r}.")
    try:
        cell = extractor(spec, ctx)
    except Exception as exc:
        log.warning("whitepaper_cell_failed", cell=spec.id, error=str(exc))
        return _analyst(
            spec, [], f"Populator error while building this cell ({type(exc).__name__})."
        )
    # Manual cells NEVER carry a generated value (INV-3) — enforce structurally.
    if spec.mode is CellMode.MANUAL and cell["value"] is not None:
        cell = {**cell, "status": "analyst_input_required", "value": None}
    return _enforce_structured_citations(cell, ctx.known_tokens)


def _build_sections(ctx: _Ctx) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for title in section_order():
        cells = [_build_cell(spec, ctx) for spec in specs_for_section(title)]
        sections.append({"title": title, "cells": cells})
    return sections


def _status_counts(sections: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"populated": 0, "analyst_input_required": 0, "verified_absent": 0}
    for section in sections:
        for cell in section["cells"]:
            counts[cell["status"]] = counts.get(cell["status"], 0) + 1
    return counts


def _fingerprint_default(o: Any) -> str:
    if isinstance(o, datetime | date):
        return o.isoformat()
    return str(o)


def result_fingerprint(sections: Any) -> str:
    """SHA-256 over the canonical JSON of the white-paper sections.

    Lets POST /whitepaper/docx verify that a client-echoed result body matches
    what the audited /whitepaper run actually produced, so a tampered cell
    value, status, or evidence list can never be rendered into an official
    document (INV-1/INV-4). Only ``sections`` (the substantive cited content) is
    fingerprinted — ``spine.application_number`` is intentionally caller-
    reformattable (it is regex-guarded where it reaches a response header).
    datetimes serialize via ``isoformat`` to match FastAPI's response encoding
    so the build-time and render-time hashes agree on the same bytes.
    """
    canon = json.dumps(
        sections,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_fingerprint_default,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _spine_from_ctx(ctx: _Ctx) -> dict[str, Any]:
    """The canonical spine for a resolved context.

    Single source of truth for the spine shape, shared by ``build_whitepaper``
    and the deterministic ``resolve_spine`` (which writes no audit row).
    """
    return {
        "application_number": ctx.appl_no,
        "application_type": ctx.application_type,
        "ingredient": ctx.ingredient,
        "normalized_name": ctx.normalized_name,
        "product_numbers": _unique(
            r.get("product_no") for r in ctx.product_rows if r.get("product_no")
        ),
        "setid": ctx.setid,
        # Additive (P1b): the full DailyMed candidate set behind the setid
        # pick, so the repackager-vs-sponsor selection is auditable and
        # overridable by the analyst -- never a silent pick.
        "spl_candidates": [
            {
                "setid": c.setid,
                "title": c.title,
                "labeler": c.labeler,
                "published": c.published,
            }
            for c in (ctx.setid_resolution.candidates if ctx.setid_resolution else ())
        ],
        "warnings": ctx.warnings,
    }


def resolve_spine(
    rld_name: str,
    application_number: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Resolve an RLD name + application number to the canonical spine ONLY.

    Runs the SAME deterministic entity resolution ``build_whitepaper`` does
    (``_build_context``), but populates no cells, calls no LLM, and writes NO
    ``log_query`` audit row (success or failure) — it is not an LLM turn. On an
    unresolved or mismatched application it raises ``SpineResolutionError``
    (the API maps it to 422 with ``.detail`` — refuse over guess).

    Like ``build_whitepaper`` it performs live FDA fetches and refreshes the
    Orange Book / SPL provenance snapshot (via ``_build_context`` → ``_persist``);
    it simply records no audit row. The shared fetch-phase deadline applies here
    too: a breach raises ``WhitepaperBuildTimeoutError`` (504 on the API) with,
    consistently, no audit row.
    """
    ctx = _build_context(rld_name, application_number, user_id=user_id)
    return _spine_from_ctx(ctx)


def _log_query_safe(**kwargs: Any) -> None:
    """``log_query`` with a DEFINED failure: never raise.

    Mirrors ``assemble.dossier._log_query_safe``: on the deadline failure path
    the audit write must not replace the typed timeout with a naked 500 -- a
    stalled DB may be the very reason the build ran long. Log + capture and
    return on any audit-write failure.
    """
    try:
        log_query(**kwargs)
    except Exception as exc:
        log.warning("whitepaper_audit_write_failed", error_type=type(exc).__name__)
        from regwatch.common.observability import capture_exception

        capture_exception(exc)


def build_whitepaper(
    rld_name: str,
    application_number: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Build the white-paper wire payload for an RLD name + application number.

    Writes exactly one ``log_query`` audit row (mode="whitepaper") on success
    AND on resolution failure (re-raising ``SpineResolutionError`` after the
    audit row) AND on a build-deadline breach (re-raising
    ``WhitepaperBuildTimeoutError``, its own route_json reason). The
    PSG-Requirements cell's scoped ``ask()`` writes its own audit row (like
    the dossier's inner Q&A).
    """
    model_name = current_model_name(role="synthesizer")
    query_text = f"whitepaper rld_name={rld_name!r} application_number={application_number!r}"
    route_json: dict[str, Any] = {
        "route": "whitepaper",
        "rld_name": rld_name,
        "application_number": application_number,
    }
    try:
        ctx = _build_context(rld_name, application_number, user_id=user_id)
    except SpineResolutionError as exc:
        log_query(
            mode="whitepaper",
            query_text=query_text,
            retrieved=[],
            answer_text=exc.detail,
            citations=[],
            refused=True,
            model_name=model_name,
            user_id=user_id,
            status="resolution_failed",
            route_json={**route_json, "reason": "spine_unresolved"},
        )
        raise
    except WhitepaperBuildTimeoutError as exc:
        # Same audited failure path as an unresolved spine -- one refused
        # mode="whitepaper" row (status="error", the 009cc41 boundary shape)
        # with its own reason, then re-raise for the API to map to 504.
        _log_query_safe(
            mode="whitepaper",
            query_text=query_text,
            retrieved=[],
            answer_text=exc.detail,
            citations=[],
            refused=True,
            model_name=model_name,
            user_id=user_id,
            status="error",
            route_json={
                **route_json,
                "reason": "build_deadline_exceeded",
                "error_type": type(exc).__name__,
            },
        )
        raise

    sections = _build_sections(ctx)
    counts = _status_counts(sections)
    spine = _spine_from_ctx(ctx)
    answer_text = (
        f"White paper for {ctx.application_type} {ctx.appl_no} ({ctx.ingredient or 'n/a'}): "
        f"{counts['populated']} populated, {counts['analyst_input_required']} analyst-input, "
        f"{counts['verified_absent']} verified-absent."
    )
    sections_sha256 = result_fingerprint(sections)
    audit_id = log_query(
        mode="whitepaper",
        query_text=query_text,
        retrieved=[],
        answer_text=answer_text,
        citations=[],
        refused=False,
        model_name=model_name,
        user_id=user_id,
        status="populated",
        route_json={
            **route_json,
            "reason": "populated",
            **counts,
            "sections_sha256": sections_sha256,
        },
    )
    return {
        "spine": spine,
        "sections": sections,
        "warnings": ctx.warnings,
        "audit_id": audit_id,
    }


# Re-exports used by tests / the docx writer.
__all__ = [
    "CELL_SPECS",
    "EXTRACTORS",
    "SpineResolutionError",
    "WhitepaperBuildTimeoutError",
    "build_whitepaper",
    "resolve_spine",
]
