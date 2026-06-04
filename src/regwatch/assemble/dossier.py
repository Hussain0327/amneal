"""Dossier builder — assemble a cited regulatory brief for a target product.

Inputs: active ingredient + (optional) dosage form + (optional) RLD.

Sections (each line carries a source link):
  A. Matched PSG(s) and extracted BE requirements (with field-level citations)
  B. RLD label (openFDA `/drug/label.json`)
  C. Applicable guidances surfaced via retrieval (cited)
  D. Dissolution method link (Dissolution Methods Database)
  E. Requirements checklist scaffold — derived from the PSG's structured fields

This is a SCAFFOLD of what the PSG calls for. It does NOT assert what the
company has done. (Spec §10.15.)
"""

from __future__ import annotations

from typing import Any

import httpx
from config.settings import get_settings
from sqlalchemy import desc
from sqlmodel import select

from regwatch.common.logging import get_logger
from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.generate.grounded_qa import ask
from regwatch.store.db import session_scope
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion

log = get_logger(__name__)

OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
DISSOLUTION_DB_URL = "https://www.accessdata.fda.gov/scripts/cder/dissolution/dsp_SearchResults.cfm"


def _find_matching_psgs(active_ingredient: str, dosage_form: str | None) -> list[dict[str, Any]]:
    """Find PSG documents matching ingredient (and optionally dosage form)."""
    canon = canonical_name(active_ingredient)
    strip = stripped_name(active_ingredient)
    matches: list[dict[str, Any]] = []
    with session_scope() as s:
        rows = list(s.scalars(select(PsgDocument)))
        for d in rows:
            d_strip = stripped_name(d.active_ingredient or "")
            if d.normalized_name == canon or d_strip == strip:
                if (
                    dosage_form
                    and d.dosage_form
                    and dosage_form.lower() not in d.dosage_form.lower()
                ):
                    continue
                matches.append(
                    {
                        "id": d.id,
                        "active_ingredient": d.active_ingredient,
                        "dosage_form": d.dosage_form,
                        "route": d.route,
                        "source_url": d.source_url,
                        "psg_type": d.psg_type,
                        "recommended_date": d.recommended_date,
                    }
                )
    return matches


def _be_requirements_for_doc(doc_id: int) -> dict[str, Any] | None:
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(BeRequirement)
                .where(BeRequirement.psg_document_id == doc_id)
                .order_by(desc(BeRequirement.version_id), desc(BeRequirement.id))  # type: ignore[arg-type]
            )
        )
        if not rows:
            return None
        be = rows[0]
        return {
            "fields": dict(be.fields_json),
            "citations": dict(be.citations_json),
            "version_id": be.version_id,
        }


def _latest_psg_version_summary(doc_id: int) -> str | None:
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(PsgVersion)
                .where(PsgVersion.psg_document_id == doc_id)
                .order_by(desc(PsgVersion.captured_at), desc(PsgVersion.id))  # type: ignore[arg-type]
                .limit(1)
            )
        )
        if not rows:
            return None
        return rows[0].diff_summary


def _fetch_rld_label(active_ingredient: str, rld: str | None) -> dict[str, Any] | None:
    """Fetch one RLD label record from openFDA. Returns None on any failure."""
    s = get_settings()
    if rld and rld.isdigit():
        query = f'openfda.application_number:"NDA{rld}"'
    else:
        # Search by generic name (active ingredient)
        query = f'openfda.generic_name:"{stripped_name(active_ingredient)}"'
    params: dict[str, Any] = {"search": query, "limit": 1}
    if s.openfda_api_key:
        params["api_key"] = s.openfda_api_key
    try:
        with httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent}) as c:
            resp = c.get(OPENFDA_LABEL_URL, params=params)
        if resp.status_code != 200:
            return None
        results = resp.json().get("results") or []
        if not results:
            return None
        r = results[0]
        openfda = r.get("openfda", {}) or {}
        return {
            "brand_name": (openfda.get("brand_name") or [None])[0],
            "generic_name": (openfda.get("generic_name") or [None])[0],
            "application_number": (openfda.get("application_number") or [None])[0],
            "indications_and_usage": " ".join(r.get("indications_and_usage") or [])[:1500],
            "dosage_and_administration": " ".join(r.get("dosage_and_administration") or [])[:1500],
            "source_url": (f"{OPENFDA_LABEL_URL}?search={query}" if query else OPENFDA_LABEL_URL),
        }
    except Exception as exc:
        log.warning("rld_label_fetch_failed", error=str(exc))
        return None


def _applicable_guidance_question(active_ingredient: str, dosage_form: str | None) -> str:
    df = f" {dosage_form}" if dosage_form else ""
    return (
        f"What are the recommended bioequivalence study design and acceptance criteria "
        f"for {active_ingredient}{df} generic products?"
    )


def _checklist_from_be_fields(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a requirements checklist scaffold from extracted BE fields."""
    items = []
    if fields.get("study_type"):
        items.append({"item": f"Study type: {fields['study_type']}", "field": "study_type"})
    if fields.get("study_design"):
        items.append({"item": f"Study design: {fields['study_design']}", "field": "study_design"})
    if fields.get("strengths"):
        items.append({"item": f"Strengths covered: {fields['strengths']}", "field": "strengths"})
    if fields.get("dissolution"):
        items.append({"item": f"Dissolution: {fields['dissolution']}", "field": "dissolution"})
    if fields.get("waiver_conditions"):
        items.append(
            {
                "item": f"Waiver conditions: {fields['waiver_conditions']}",
                "field": "waiver_conditions",
            }
        )
    if fields.get("additional_notes"):
        items.append({"item": f"Notes: {fields['additional_notes']}", "field": "additional_notes"})
    return items


def build_dossier(
    *,
    active_ingredient: str,
    dosage_form: str | None,
    rld: str | None,
) -> dict[str, Any]:
    """Assemble a cited dossier. If no matching PSG is in our store, refuses."""
    psg_matches = _find_matching_psgs(active_ingredient, dosage_form)
    if not psg_matches:
        return {
            "markdown": (
                f"# {active_ingredient} dossier\n\n"
                f"No PSG for this product is present in the current corpus. "
                f"Run `uv run regwatch seed` (or a broader ingest) and retry. "
                f"This system never invents PSG content."
            ),
            "sections": {"matched_psgs": []},
            "refused": True,
        }

    # Section A — matched PSGs + BE requirements
    psgs_section: list[dict[str, Any]] = []
    md_lines: list[str] = [f"# {active_ingredient} dossier", ""]
    md_lines.append("## A. Product-Specific Guidance(s)")
    for p in psg_matches:
        psg_id = p["id"]
        if not isinstance(psg_id, int):
            continue
        be = _be_requirements_for_doc(psg_id)
        version_summary = _latest_psg_version_summary(psg_id)
        md_lines.append(
            f"- **{p['active_ingredient']}** ({p['dosage_form']}; {p['route']}) "
            f"— [{p['psg_type']}, recommended {p['recommended_date'] or 'n/a'}]"
            f"({p['source_url']})"
        )
        if version_summary:
            md_lines.append(f"  - Latest change: {version_summary}")
        psgs_section.append({"psg": p, "be_requirements": be, "diff_summary": version_summary})

    md_lines.append("")
    md_lines.append("## B. Extracted BE Requirements")
    for ps in psgs_section:
        be = ps["be_requirements"]
        if not be:
            md_lines.append(f"- *No structured BE extraction yet for {ps['psg']['source_url']}.*")
            continue
        md_lines.append(f"### From {ps['psg']['source_url']}")
        for field, value in (be["fields"] or {}).items():
            if not value:
                continue
            cite = (be["citations"] or {}).get(field)
            page = cite.get("page") if isinstance(cite, dict) else None
            quote = cite.get("quote") if isinstance(cite, dict) else None
            page_str = f" [{ps['psg']['source_url']}#page={page}]" if page else ""
            md_lines.append(f"- **{field}**: {value}{page_str}")
            if quote:
                md_lines.append(f"    > {quote}")

    # Section C — RLD label
    rld_label = _fetch_rld_label(active_ingredient, rld)
    md_lines.append("")
    md_lines.append("## C. Reference Listed Drug (RLD) Label")
    if rld_label:
        md_lines.append(
            f"- Brand: {rld_label['brand_name']}  /  Generic: {rld_label['generic_name']}"
        )
        md_lines.append(f"- Application: {rld_label['application_number']}")
        md_lines.append(f"- Source: {rld_label['source_url']}")
        if rld_label.get("indications_and_usage"):
            md_lines.append(f"  - **Indications**: {rld_label['indications_and_usage'][:400]}…")
    else:
        md_lines.append("- *RLD label not available from openFDA (or rate-limited).*")

    # Section D — Applicable guidances (retrieval-driven, cited)
    md_lines.append("")
    md_lines.append("## D. Applicable Guidance — Q&A Summary")
    # Pin the product so the applicable-guidance Q&A can't pull another drug's
    # PSG chunks (cross-drug-leak guard — INV-1).
    qa = ask(
        _applicable_guidance_question(active_ingredient, dosage_form),
        filters={"normalized_name": canonical_name(active_ingredient)},
    )
    md_lines.append(qa.answer)
    if qa.citations:
        md_lines.append("")
        md_lines.append("### Sources")
        for c in qa.citations:
            md_lines.append(f"- {c.short_name}, p.{c.page}: {c.source_url}")

    # Section E — Dissolution methods link
    md_lines.append("")
    md_lines.append("## E. Dissolution Method")
    md_lines.append(f"- See FDA Dissolution Methods Database: {DISSOLUTION_DB_URL}")

    # Section F — Requirements checklist scaffold
    md_lines.append("")
    md_lines.append("## F. Requirements Checklist (scaffold)")
    md_lines.append(
        "_This is what the PSG calls for. It does not assert what the company has done._"
    )
    for ps in psgs_section:
        be = ps["be_requirements"]
        if not be:
            continue
        for item in _checklist_from_be_fields(be["fields"]):
            md_lines.append(f"- [ ] {item['item']} ({item['field']})")

    return {
        "markdown": "\n".join(md_lines),
        "sections": {
            "matched_psgs": psgs_section,
            "rld_label": rld_label,
            "qa_answer": qa.answer,
            "qa_citations": [c.__dict__ for c in qa.citations],
            "qa_refused": qa.refused,
        },
        "refused": False,
    }
