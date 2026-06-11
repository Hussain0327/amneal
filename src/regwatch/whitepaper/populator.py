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

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc
from sqlmodel import select

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
from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.generate.grounded_qa import ask
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
from regwatch.store.whitepaper_sources import (
    persist_ob_exclusivities,
    persist_ob_patents,
    persist_ob_products,
    persist_spl_document,
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


def _rems_records(query: SourceQuery) -> list[SourceRecord]:
    return _rems_search(query)[0]


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
    ob_fetched_at: datetime | None = None
    ob_failed: bool = False
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
    psg_failed: bool = False
    be_requirement: dict[str, Any] | None = None
    known_tokens: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


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


def _name_matches(rld_name: str, candidates: Iterable[str]) -> bool:
    want_canon = canonical_name(rld_name)
    want_strip = stripped_name(rld_name)
    want_lc = " ".join(rld_name.lower().split())
    if not want_lc:
        return True  # nothing to disagree with
    for cand in candidates:
        cand_lc = " ".join((cand or "").lower().split())
        if not cand_lc:
            continue
        if want_canon and canonical_name(cand) == want_canon:
            return True
        if want_strip and stripped_name(cand) == want_strip:
            return True
        if want_lc in cand_lc or cand_lc in want_lc:
            return True
    return False


def _build_context(rld_name: str, application_number: str, *, user_id: str | None) -> _Ctx:
    appl_no, input_type = _split_input(application_number)
    now = datetime.now(UTC)
    ctx = _Ctx(
        rld_name=rld_name,
        application_number_input=application_number,
        appl_no=appl_no,
        application_type=input_type or "NDA",
        ingredient="",
        normalized_name="",
        now=now,
        user_id=user_id,
    )

    _fetch_orange_book(ctx)
    _fetch_drugsfda(ctx)
    _establish_identity(ctx, input_type)
    _filter_rows_to_application_type(ctx)
    _reconcile_rld_name(ctx)
    _fetch_dailymed(ctx)
    _fetch_ndc(ctx)
    _fetch_psg_store(ctx)
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
    ctx.ob_fetched_at = products.fetched_at


def _fetch_drugsfda(ctx: _Ctx) -> None:
    try:
        ctx.drugsfda_records = _drugsfda_records(
            SourceQuery(application_number=ctx.application_number_input, limit=5)
        )
    except Exception as exc:
        ctx.warnings.append(f"Drugs@FDA fetch failed ({type(exc).__name__}).")
        log.warning("whitepaper_drugsfda_fetch_failed", error=str(exc))


def _establish_identity(ctx: _Ctx, input_type: str) -> None:
    if not input_type:
        _reject_cross_type_ambiguity(ctx)
    ingredient = ""
    if ctx.product_rows:
        ingredient = ctx.product_rows[0].get("ingredient", "")
        letter = ctx.product_rows[0].get("appl_type", "")
        ctx.application_type = _OB_TYPE_TO_APP.get(letter, ctx.application_type)
    if not ingredient:
        ingredient = _drugsfda_ingredient(ctx.drugsfda_records)
    if not ctx.product_rows and not ctx.drugsfda_records:
        raise SpineResolutionError(
            f"No Orange Book or Drugs@FDA product found for application number "
            f"{ctx.application_type} {ctx.appl_no}. Confirm the number is correct."
        )
    if not input_type:
        ctx.application_type = _type_from_drugsfda(ctx.drugsfda_records) or ctx.application_type
    ctx.ingredient = ingredient
    ctx.normalized_name = canonical_name(ingredient) if ingredient else ""


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
    try:
        resolution = dailymed.resolve_setid(ctx.application_number_input, prefer_titles=prefer)
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
            SourceQuery(application_number=ctx.application_number_input, limit=20)
        )
    except Exception as exc:
        ctx.ndc_records = None
        ctx.warnings.append(f"NDC Directory fetch failed ({type(exc).__name__}).")
        log.warning("whitepaper_ndc_failed", error=str(exc))


def _fetch_psg_store(ctx: _Ctx) -> None:
    try:
        ctx.psg_docs = _matching_psg_docs(ctx)
        ctx.be_requirement = _latest_be_requirement(ctx.psg_docs)
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


def _type_from_drugsfda(records: list[SourceRecord]) -> str | None:
    for rec in records:
        an = rec.identifiers.get("application_number", "")
        for prefix in ("NDA", "ANDA", "BLA"):
            if an.startswith(prefix):
                return prefix
    return None


def _matching_psg_docs(ctx: _Ctx) -> list[dict[str, Any]]:
    if not ctx.normalized_name and not ctx.appl_no:
        return []
    canon = ctx.normalized_name
    strip = stripped_name(ctx.ingredient) if ctx.ingredient else ""
    out: list[dict[str, Any]] = []
    with session_scope() as s:
        for d in s.scalars(select(PsgDocument)):
            d_strip = stripped_name(d.active_ingredient or "")
            by_name = bool(canon) and (
                d.normalized_name == canon or (bool(strip) and d_strip == strip)
            )
            by_appl = bool(d.appl_no) and d.appl_no == ctx.appl_no
            by_ref = bool(d.rld_or_rs_number) and ctx.appl_no in (d.rld_or_rs_number or "")
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
                    }
                )
    return out


def _latest_be_requirement(psg_docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    doc_ids = [d["id"] for d in psg_docs if isinstance(d.get("id"), int)]
    if not doc_ids:
        return None
    with session_scope() as s:
        row = s.scalars(
            select(BeRequirement)
            .where(BeRequirement.psg_document_id.in_(doc_ids))  # type: ignore[attr-defined]
            .order_by(desc(BeRequirement.version_id), desc(BeRequirement.id))  # type: ignore[arg-type]
        ).first()
        if row is None:
            return None
        url = next(
            (d["source_url"] for d in psg_docs if d.get("id") == row.psg_document_id),
            None,
        )
        return {
            "fields": dict(row.fields_json) or _be_fields(row),
            "citations": dict(row.citations_json),
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
    try:
        if not ctx.ob_failed:
            # Replace-snapshot only on a SUCCESSFUL fetch — a failed query must
            # never wipe the previous durable snapshot.
            fetched_at = ctx.ob_fetched_at or ctx.now
            persist_ob_products(
                ctx.appl_no,
                ctx.product_rows,
                fetched_at=fetched_at,
                source_url=ORANGE_BOOK_SEARCH_URL,
            )
            persist_ob_patents(
                ctx.appl_no,
                ctx.patent_rows,
                fetched_at=fetched_at,
                source_url=ORANGE_BOOK_SEARCH_URL,
            )
            persist_ob_exclusivities(
                ctx.appl_no,
                ctx.exclusivity_rows,
                fetched_at=fetched_at,
                source_url=ORANGE_BOOK_SEARCH_URL,
            )
        if ctx.setid_resolution is not None:
            resolution = ctx.setid_resolution
            persist_spl_document(
                setid=resolution.setid,
                appl_no=ctx.appl_no,
                title=resolution.title,
                published=resolution.published,
                source_url=resolution.source_url,
                fetched_at=ctx.spl_fetched_at or resolution.fetched_at,
            )
    except Exception as exc:
        ctx.warnings.append("Persistence write-through failed (provenance rows not stored).")
        log.warning("whitepaper_persist_failed", error=str(exc))


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
                fetched_at=ctx.ob_fetched_at,
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
    # Identity filter: query by application number ONLY — an ingredient search
    # could return another product's shortage and emit a false "Yes".
    query = SourceQuery(application_number=ctx.application_number_input, limit=10)
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
    query = SourceQuery(
        application_number=ctx.application_number_input,
        active_ingredient=ctx.ingredient or None,
        brand_name=brand or None,
        limit=10,
    )
    try:
        records, total_rows = _rems_search(query)
    except Exception as exc:
        return _analyst(
            spec,
            [],
            f"REMS index query failed ({type(exc).__name__}); cannot assert REMS status "
            f"(tri-state, INV-5).",
        )
    if records:
        confirmed = [r for r in records if _rems_record_matches_application(r, ctx.appl_no)]
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


def _rems_record_matches_application(record: SourceRecord, appl_no: str) -> bool:
    """True when the index row itself carries this application number.

    Single-product programs embed the number in free text ("NDA #022549");
    shared-system rows carry none and stay ambiguous.
    """
    rec_no = record.identifiers.get("application_number") or ""
    if rec_no.endswith(appl_no):
        return True
    return any(appl_no in str(v) for v in record.raw.values())


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
    assert ctx.setid is not None
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
    assert ctx.setid is not None
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
    assert ctx.setid is not None
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
    return _verified_absent(
        spec,
        [
            _evidence(
                "PSG store",
                f"appl_no={ctx.appl_no}",
                fetched_at=ctx.now,
                snippet="No PSG in the local store keyed to this application number/ingredient.",
            )
        ],
        "Local PSG store queried; no product-specific guidance present.",
    )


def _ext_psg_requirements(spec: CellSpec, ctx: _Ctx) -> dict[str, Any]:
    if not ctx.normalized_name:
        return _analyst(spec, [], "No normalized ingredient resolved; cannot scope the PSG ask.")
    question = (
        f"What are the recommended bioequivalence study design and acceptance criteria for "
        f"{ctx.ingredient} generic products?"
    )
    try:
        qa = ask(
            question,
            filters={"normalized_name": ctx.normalized_name},
            user_id=ctx.user_id,
            bind_session=False,
        )
    except Exception as exc:
        return _analyst(spec, [], f"Scoped PSG ask failed ({type(exc).__name__}).")
    if qa.status == "clarify":
        return _analyst(
            spec,
            [],
            "PSG corpus spans more than one dosage form for this ingredient; analyst must "
            "select the form (forms are not blended, INV-1).",
        )
    if qa.refused or qa.status == "refused":
        return _analyst(
            spec,
            [],
            "Scoped PSG ask refused — the ingredient is not in the corpus or retrieval was "
            "below threshold (INV-9).",
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
        return _analyst(spec, [], "Scoped PSG ask produced no validated citations (INV-1).")
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
                fetched_at=ctx.ob_fetched_at,
                snippet=_patent_snippet(row),
            )
        )
    note = (
        "Orange Book patent rows surfaced as evidence; paragraph classification / priority "
        "posture is regulatory judgment (INV-3)."
        if ev
        else "No Orange Book patent rows for this application; patent posture is analyst "
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
                fetched_at=ctx.ob_fetched_at,
                snippet=f"code {code} (expires {row.get('exclusivity_date') or 'n/a'})",
            )
        )
    note = (
        "Orange Book exclusivity rows surfaced as evidence; First-to-Market / eFTF eligibility "
        "is regulatory judgment (INV-3)."
        if ev
        else "No Orange Book exclusivity rows for this application; eligibility is analyst "
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
    query = SourceQuery(
        application_number=ctx.application_number_input,
        active_ingredient=ctx.ingredient or None,
        limit=10,
    )
    ev: list[dict[str, Any]] = []
    note_tail = ""
    try:
        for rec in _rems_records(query):
            ev.append(
                _evidence(
                    "REMS@FDA",
                    rec.identifiers.get("application_number") or "rems index",
                    source_url=rec.source_url or REMS_INDEX_URL,
                    fetched_at=ctx.now,
                    snippet=rec.title,
                )
            )
    except Exception as exc:
        note_tail = f" REMS query failed ({type(exc).__name__})."
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
                    fetched_at=ctx.ob_fetched_at,
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
    note = (
        "Per-study requirement is analyst judgment (INV-3); the PSG field(s) and citation are "
        "surfaced as evidence."
    )
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


def build_whitepaper(
    rld_name: str,
    application_number: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Build the white-paper wire payload for an RLD name + application number.

    Writes exactly one ``log_query`` audit row (mode="whitepaper") on success
    AND on resolution failure (re-raising ``SpineResolutionError`` after the
    audit row). The PSG-Requirements cell's scoped ``ask()`` writes its own
    audit row (like the dossier's inner Q&A).
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

    sections = _build_sections(ctx)
    counts = _status_counts(sections)
    spine = {
        "application_number": ctx.appl_no,
        "application_type": ctx.application_type,
        "ingredient": ctx.ingredient,
        "normalized_name": ctx.normalized_name,
        "product_numbers": _unique(
            r.get("product_no") for r in ctx.product_rows if r.get("product_no")
        ),
        "setid": ctx.setid,
        "warnings": ctx.warnings,
    }
    answer_text = (
        f"White paper for {ctx.application_type} {ctx.appl_no} ({ctx.ingredient or 'n/a'}): "
        f"{counts['populated']} populated, {counts['analyst_input_required']} analyst-input, "
        f"{counts['verified_absent']} verified-absent."
    )
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
        route_json={**route_json, "reason": "populated", **counts},
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
    "build_whitepaper",
]
