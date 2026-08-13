"""Dossier builder — assemble a cited regulatory brief for a target product.

Inputs: active ingredient + (optional) dosage form + (optional) RLD.

Sections (each line carries a source link):
  A. Matched PSG(s) and extracted BE requirements (with field-level citations)
  B. RLD approved label from Drugs@FDA
  C. Applicable guidances surfaced via retrieval (cited)
  D. Dissolution method link (Dissolution Methods Database)
  E. Requirements checklist scaffold — derived from the PSG's structured fields

This is a SCAFFOLD of what the PSG calls for. It does NOT assert what the
company has done. (Spec §10.15.)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import desc
from sqlalchemy import text as sa_text
from sqlmodel import select

from regwatch.common.audit import log_query
from regwatch.common.logging import get_logger
from regwatch.common.observability import capture_exception
from regwatch.common.text_normalize import canonical_name, names_match, stripped_name
from regwatch.generate.grounded_qa import ask
from regwatch.generate.llm import current_model_name
from regwatch.store.db import session_scope
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion

log = get_logger(__name__)


def _escape_lucene_phrase(value: str) -> str:
    """Deprecated pure escape helper retained for stored-client compatibility.

    The application no longer constructs Lucene queries or calls the retired
    API; keeping this side-effect-free function avoids breaking code that only
    imported its escaping behavior.
    """

    return value.replace("\\", "\\\\").replace('"', '\\"')


def _form_tokens(value: str) -> set[str]:
    """Significant dosage-form tokens, dropping short connector words."""
    return {t for t in value.lower().replace(",", " ").split() if len(t) > 2}


# Release-type / administration modifiers that mark a *clinically distinct* PSG
# (its BE recommendations differ), so a plain form must never lenient-match a
# sibling that carries one — "Tablet" must not pull in "Tablet, Extended
# Release" just because "tablet" is a substring/shared token of it (INV-5).
# "immediate"/"ir" and bare route words are intentionally ABSENT: a plain
# "Tablet" already means immediate-release, so excluding it would only cause
# false "no PSG found" refusals. Mirrors whitepaper.populator._form_compatible's
# release-type guard; keep the two in lockstep like names_match already is.
_FORM_MODIFIERS = frozenset(
    {
        "extended",
        "delayed",
        "sustained",
        "controlled",
        "modified",
        "chewable",
        "disintegrating",
        "effervescent",
        "sublingual",
        "buccal",
    }
)
# Common abbreviations normalized to the spelled-out modifier so "Tablet ER" and
# "Tablet, Extended Release" compare equal.
_MODIFIER_ALIASES = {
    "er": "extended",
    "xr": "extended",
    "xl": "extended",
    "dr": "delayed",
    "sr": "sustained",
    "cr": "controlled",
    "odt": "disintegrating",
}


def _form_modifiers(value: str) -> set[str]:
    """Release-type/administration modifier tokens present in a form string.

    Tokenizes without the ``_form_tokens`` length filter so 2-char abbreviations
    (ER/XR/DR/...) are seen, then normalizes them to their spelled-out modifier.
    """
    toks = {_MODIFIER_ALIASES.get(t, t) for t in value.lower().replace(",", " ").split()}
    return toks & _FORM_MODIFIERS


def _log_query_safe(**kwargs: Any) -> None:
    """``log_query`` with a DEFINED failure: never raise.

    INV-6 durability must not itself crash the /assemble response — a still-down
    DB inside the error handler would otherwise reproduce the very gap this
    closes. Mirrors grounded_qa._log_query_or_skip: log + Sentry-capture and
    return on any audit-write failure.
    """
    try:
        log_query(**kwargs)
    except Exception as exc:
        log.warning("assemble_audit_write_failed", error_type=type(exc).__name__)
        capture_exception(exc)


def _find_matching_psgs(active_ingredient: str, dosage_form: str | None) -> list[dict[str, Any]]:
    """Find PSG documents matching ingredient (and optionally dosage form)."""
    canon = canonical_name(active_ingredient)
    strip = stripped_name(active_ingredient)
    matches: list[dict[str, Any]] = []
    with session_scope() as s:
        rows = list(s.scalars(select(PsgDocument)))
        for d in rows:
            if names_match(canon, strip, d.normalized_name, d.active_ingredient):
                if dosage_form and d.dosage_form:
                    want = dosage_form.lower()
                    have = d.dosage_form.lower()
                    # Lenient match: keep if the query form is a substring OR shares
                    # a form token, so "inhalation aerosol" matches "Aerosol, Metered"
                    # while "tablet" is still correctly excluded.
                    if want not in have and not (_form_tokens(want) & _form_tokens(have)):
                        continue
                    # ...but never blend across a release-type/administration
                    # modifier the other side lacks: a plain "Tablet" query must
                    # not admit "Tablet, Extended Release" (a distinct PSG with
                    # different BE requirements) just because it is a substring/
                    # shared token of it (INV-5).
                    if _form_modifiers(want) != _form_modifiers(have):
                        continue
                matches.append(
                    {
                        "id": d.id,
                        "active_ingredient": d.active_ingredient,
                        "normalized_name": d.normalized_name,
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
    """Read one current approved-label document from the FDA-only corpus."""
    digits = "".join(character for character in (rld or "") if character.isdigit())
    normalized = canonical_name(active_ingredient)
    try:
        with session_scope() as session:
            rows = (
                session.connection()
                .execute(
                    sa_text(
                        "SELECT DISTINCT ON (d.id) d.id, d.brand_name, d.active_ingredient, "
                        "d.normalized_name, d.application_number, d.source_url, "
                        "c.fda_version_id FROM fda_document d JOIN chunk c "
                        "ON c.fda_document_id = d.id WHERE d.is_active "
                        "AND d.document_type = 'approved_label' "
                        "AND (:digits = '' OR regexp_replace(COALESCE(d.application_number, ''), "
                        "'[^0-9]', '', 'g') = :digits) "
                        "ORDER BY d.id, c.fda_version_id DESC"
                    ),
                    {"digits": digits.zfill(6) if digits else ""},
                )
                .mappings()
                .all()
            )
            if not digits:
                rows = [
                    row
                    for row in rows
                    if names_match(
                        normalized,
                        stripped_name(active_ingredient),
                        str(row["normalized_name"] or ""),
                        str(row["active_ingredient"] or ""),
                    )
                ]
            if not rows:
                return None
            label = rows[0]
            chunks = (
                session.connection()
                .execute(
                    sa_text(
                        "SELECT COALESCE(page, 1) AS page, text FROM chunk "
                        "WHERE fda_document_id = :document_id "
                        "AND fda_version_id = :version_id ORDER BY page, ordinal, id"
                    ),
                    {
                        "document_id": label["id"],
                        "version_id": label["fda_version_id"],
                    },
                )
                .mappings()
                .all()
            )
        indications, indications_page = _label_excerpt(chunks, "indications and usage")
        dosage, dosage_page = _label_excerpt(chunks, "dosage and administration")
        return {
            "brand_name": label["brand_name"],
            "generic_name": label["active_ingredient"],
            "application_number": label["application_number"],
            "indications_and_usage": indications,
            "indications_page": indications_page,
            "dosage_and_administration": dosage,
            "dosage_page": dosage_page,
            "source_url": label["source_url"],
        }
    except Exception as exc:
        log.warning("rld_label_fetch_failed", error=str(exc))
        return None


def _label_excerpt(rows: Sequence[Any], heading: str) -> tuple[str, int | None]:
    for row in rows:
        text = str(row["text"] or "")
        offset = text.lower().find(heading)
        if offset >= 0:
            return text[offset : offset + 1_500], int(row["page"])
    return "", None


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


def _assemble_query_text(active_ingredient: str, dosage_form: str | None, rld: str | None) -> str:
    """Human-readable record of the assemble inputs for the audit log (INV-6)."""
    parts = [f"assemble active_ingredient={active_ingredient}"]
    parts.append(f"dosage_form={dosage_form or 'n/a'}")
    parts.append(f"rld={rld or 'n/a'}")
    return " ".join(parts)


def build_dossier(
    *,
    active_ingredient: str,
    dosage_form: str | None,
    rld: str | None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Assemble a cited dossier. If no matching PSG is in our store, refuses.

    INV-6: every assemble — refused, assembled, OR errored mid-build — must leave
    exactly one durable mode="assemble" row. The heavy work runs inside an error
    boundary so a DB read or the inner ask() raising (e.g. a transient Postgres
    blip, the Jun-18 incident class) still writes a status="error" row and
    degrades to a clean refusal instead of a bare 500 with no audit trail.
    """
    model_name = current_model_name(role="synthesizer")
    query_text = _assemble_query_text(active_ingredient, dosage_form, rld)
    route_json: dict[str, Any] = {
        "route": "assemble_dossier",
        "active_ingredient": active_ingredient,
        "dosage_form": dosage_form,
        "rld": rld,
    }
    try:
        return _assemble_dossier(
            active_ingredient=active_ingredient,
            dosage_form=dosage_form,
            rld=rld,
            user_id=user_id,
            model_name=model_name,
            query_text=query_text,
            route_json=route_json,
        )
    except Exception as exc:
        # An error anywhere in assembly must still leave an audit row (INV-6),
        # mirroring ask()'s status="error" degrade. The write is failure-safe so
        # a still-down DB can't re-raise from inside this handler.
        log.warning("assemble_failed", error_type=type(exc).__name__)
        capture_exception(exc)
        markdown = (
            f"# {active_ingredient} dossier\n\n"
            "This dossier could not be assembled right now due to a temporary "
            "error reaching the corpus or guidance service. Please retry shortly. "
            "No content was invented."
        )
        _log_query_safe(
            mode="assemble",
            query_text=query_text,
            retrieved=[],
            answer_text=markdown,
            citations=[],
            refused=True,
            model_name=model_name,
            user_id=user_id,
            status="error",
            route_json={**route_json, "reason": "error", "error_type": type(exc).__name__},
        )
        return {
            "markdown": markdown,
            "sections": {"matched_psgs": []},
            "refused": True,
        }


def _assemble_dossier(
    *,
    active_ingredient: str,
    dosage_form: str | None,
    rld: str | None,
    user_id: str | None,
    model_name: str,
    query_text: str,
    route_json: dict[str, Any],
) -> dict[str, Any]:
    """Assembly body for build_dossier — see it for the INV-6 error contract."""
    psg_matches = _find_matching_psgs(active_ingredient, dosage_form)
    if not psg_matches:
        markdown = (
            f"# {active_ingredient} dossier\n\n"
            f"No PSG for this product is present in the current corpus. "
            f"Run `uv run regwatch seed` (or a broader ingest) and retry. "
            f"This system never invents PSG content."
        )
        _log_query_safe(
            mode="assemble",
            query_text=query_text,
            retrieved=[],
            answer_text=markdown,
            citations=[],
            refused=True,
            model_name=model_name,
            user_id=user_id,
            status="refused",
            route_json={**route_json, "reason": "no_matching_psg"},
        )
        return {
            "markdown": markdown,
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
        md_lines.append("- *RLD approved label is not present in the FDA corpus.*")

    # Section D — Applicable guidances (retrieval-driven, cited)
    md_lines.append("")
    md_lines.append("## D. Applicable Guidance — Q&A Summary")
    # Pin the product so the applicable-guidance Q&A can't pull another drug's
    # PSG chunks (cross-drug-leak guard — INV-1). For a multi-form drug, also pin
    # the matched documents' exact (dosage_form, route) when they agree on one, so
    # the Q&A's multi-form guard doesn't start clarifying inside a dossier — the
    # dossier already knows the form it's building for.
    # Pin the inner Q&A to the MATCHED document's normalized_name, not the raw
    # input's canonical form. A PSG matched only via the salt-stripped name (doc
    # stored "albuterol sulfate", query "albuterol") would otherwise be pinned to
    # the salt-free canonical, which retrieval's exact-match filter can't find —
    # blanking Section D. Fall back to the canonical form when matches disagree
    # or there are none (unchanged behavior).
    matched_names = {p["normalized_name"] for p in psg_matches if p.get("normalized_name")}
    pinned_name = (
        next(iter(matched_names)) if len(matched_names) == 1 else canonical_name(active_ingredient)
    )
    qa_filters: dict[str, Any] = {"normalized_name": pinned_name}
    matched_combos = {
        (p["dosage_form"], p["route"])
        for p in psg_matches
        if p.get("dosage_form") and p.get("route")
    }
    if len(matched_combos) == 1:
        form, route = next(iter(matched_combos))
        qa_filters["dosage_form"] = form
        qa_filters["route"] = route
    # user_id rides along so the inner Q&A's audit row carries caller identity
    # too — every log_query from an authenticated request is attributed (INV-6).
    # bind_session=False keeps that attribution audit-only: the synthetic BE
    # question's ChatSession stays unowned, so it never shows up as a phantom
    # conversation in the caller's /sessions history.
    qa = ask(
        _applicable_guidance_question(active_ingredient, dosage_form),
        filters=qa_filters,
        user_id=user_id,
        bind_session=False,
    )
    if qa.status == "clarify":
        # The product spans more than one (dosage_form, route) combo and no single
        # form was pinned, so the inner Q&A asked WHICH form. A dossier is a
        # non-interactive document — never embed that dangling clarify prompt as
        # Section D content. State the forms explicitly and direct the reader to
        # rebuild per form instead of blending them (INV-1 / INV-5).
        forms = ", ".join(sorted(f"{form} ({route})" for form, route in matched_combos))
        md_lines.append(
            f"_{active_ingredient} has FDA guidance for more than one dosage form "
            f"({forms}). Rebuild this dossier with a specific dosage form to get "
            f"form-specific guidance — forms are not blended._"
        )
    else:
        md_lines.append(qa.answer)
        if qa.citations:
            md_lines.append("")
            md_lines.append("### Sources")
            for c in qa.citations:
                md_lines.append(f"- {c.short_name}, p.{c.page}: {c.source_url}")

    # Section E — dissolution requirements are limited to allowed sources.
    md_lines.append("")
    md_lines.append("## E. Dissolution Method")
    md_lines.append(
        "- See the cited PSG and FDA bioequivalence guidance above; no source "
        "outside the authoritative corpus is consulted."
    )

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

    markdown = "\n".join(md_lines)
    qa_citations = [dict(c.__dict__) for c in qa.citations]
    _log_query_safe(
        mode="assemble",
        query_text=query_text,
        retrieved=qa.retrieved,
        answer_text=markdown,
        citations=qa_citations,
        refused=False,
        model_name=model_name,
        user_id=user_id,
        status="assembled",
        route_json={**route_json, "reason": "assembled", "matched_psgs": len(psgs_section)},
    )
    return {
        "markdown": markdown,
        "sections": {
            "matched_psgs": psgs_section,
            "rld_label": rld_label,
            # On a clarify, qa.answer is the inner Q&A's dangling "which form?"
            # prompt, which the markdown deliberately does NOT embed. Don't leak
            # it here either — callers get qa_status="clarify" to detect the
            # ambiguity; the stated-forms note lives in the markdown.
            "qa_answer": None if qa.status == "clarify" else qa.answer,
            "qa_citations": qa_citations,
            "qa_refused": qa.refused,
            # Surface the inner Q&A status so API callers can detect a multi-form
            # ambiguity ("clarify") rather than silently treating Section D's note
            # as a normal answer.
            "qa_status": qa.status,
        },
        "refused": False,
    }
