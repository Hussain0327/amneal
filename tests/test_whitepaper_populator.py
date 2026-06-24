"""White-Paper populator — spine resolution, every mode, tri-state, INV-8."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from regwatch.sources import dailymed, orange_book
from regwatch.sources.dailymed import SetidResolution, SplXmlDocument
from regwatch.sources.orange_book import OrangeBookRows
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord
from regwatch.store.db import session_scope
from regwatch.store.models import BeRequirement, PsgDocument, PsgVersion
from regwatch.whitepaper import populator
from regwatch.whitepaper.populator import (
    SpineResolutionError,
    _cell,
    _Ctx,
    _enforce_structured_citations,
    _ext_psg_requirements,
    _form_compatible,
    _name_matches,
    _populated,
    _rems_record_matches_application,
    build_whitepaper,
)
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources


def _cells(result: dict) -> dict[str, dict]:
    return {c["id"]: c for s in result["sections"] for c in s["cells"]}


def _seed_psg_doc(**overrides: object) -> int:
    """Seed one PSG document directly (the stub's seeder is fixed-shape)."""
    defaults: dict[str, object] = {
        "active_ingredient": "Ibuprofen",
        "normalized_name": "ibuprofen",
        "dosage_form": "Tablet",
        "route": "Oral",
        "appl_no": None,
        "rld_or_rs_number": None,
        "psg_type": "final",
        "recommended_date": "2021-01-01",
        "source_url": "http://example/PSG_other.pdf",
        "content_hash": "hash-other",
    }
    defaults.update(overrides)
    with session_scope() as s:
        doc = PsgDocument(**defaults)  # type: ignore[arg-type]
        s.add(doc)
        s.flush()
        assert doc.id is not None
        return doc.id


def _seed_be_requirement(doc_id: int, *, dissolution: str) -> None:
    with session_scope() as s:
        version = PsgVersion(psg_document_id=doc_id, content_hash=f"hash-{doc_id}")
        s.add(version)
        s.flush()
        assert version.id is not None
        s.add(
            BeRequirement(
                psg_document_id=doc_id,
                version_id=version.id,
                dissolution=dissolution,
                fields_json={"dissolution": dissolution},
                citations_json={"dissolution": {"page": 9}},
            )
        )


# --------------------------- spine resolution ---------------------------
@pytest.mark.parametrize("appl", ["020503", "NDA 020503", "NDA020503", "N020503"])
def test_spine_resolves_format_variants(monkeypatch: pytest.MonkeyPatch, appl: str) -> None:
    install_fake_sources(monkeypatch)
    result = build_whitepaper(RLD_NAME, appl)
    spine = result["spine"]
    assert spine["application_number"] == APPL_NO
    assert spine["application_type"] == "NDA"
    assert spine["ingredient"] == "ALBUTEROL SULFATE"
    assert spine["normalized_name"] == "albuterol sulfate"
    assert spine["product_numbers"] == ["001"]
    assert spine["setid"] == "abc-def-123"


def test_spine_name_mismatch_raises_422(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    with pytest.raises(SpineResolutionError) as exc:
        build_whitepaper("ibuprofen", APPL_NO)
    # The failure lists what WAS found, never guesses.
    assert "ibuprofen" in exc.value.detail
    assert "ALBUTEROL SULFATE" in exc.value.detail or "PROVENTIL" in exc.value.detail


def test_spine_unparseable_number_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    with pytest.raises(SpineResolutionError):
        build_whitepaper(RLD_NAME, "not-a-number")


def test_spine_no_product_anywhere_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    monkeypatch.setattr(
        populator,
        "_drugsfda_records",
        lambda q: (_ for _ in ()).throw(AssertionError("should not be reached")),
    )

    # OB returns no rows and Drugs@FDA returns nothing -> cannot establish identity.
    def _empty_products(application_number: str, *, client: object = None) -> OrangeBookRows:
        return OrangeBookRows(rows=[], fetched_at=datetime.now(UTC))

    monkeypatch.setattr(orange_book, "product_rows", _empty_products)
    monkeypatch.setattr(orange_book, "patent_rows", _empty_products)
    monkeypatch.setattr(orange_book, "exclusivity_rows", _empty_products)
    monkeypatch.setattr(populator, "_drugsfda_records", lambda q: [])
    with pytest.raises(SpineResolutionError):
        build_whitepaper(RLD_NAME, "999999")


# --------------------------- auto cells ---------------------------
def test_auto_cells_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    assert cells["product_name"]["status"] == "populated"
    assert "ALBUTEROL" in cells["product_name"]["value"]
    assert cells["dosage_form"]["value"] == "AEROSOL, METERED"
    assert cells["route"]["value"] == "INHALATION"
    assert "0.09" in cells["strengths"]["value"]
    assert cells["rld"]["value"].startswith("Yes")
    assert cells["rs"]["value"].startswith("Yes")
    assert "MERCK" in cells["nda_holder"]["value"]
    assert "0085-1132-01" in cells["packaging"]["value"]
    # Every populated auto cell carries provenance (source + locator + fetched_at).
    ev = cells["product_name"]["evidence"][0]
    assert ev["source"] == "Orange Book"
    assert ev["locator"] == "OB_020503/001"
    assert ev["fetched_at"]


# --------------------------- tri-state Yes/No ---------------------------
def test_shortage_verified_absent_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, on_shortage=False)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["drug_shortage"]
    assert cell["status"] == "verified_absent"
    assert cell["value"] == "No"
    assert cell["evidence"]  # the query is recorded as evidence


def test_shortage_yes_when_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, on_shortage=True)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["drug_shortage"]
    assert cell["status"] == "populated"
    assert cell["value"].startswith("Yes")


def test_shortage_failure_collapses_to_analyst(monkeypatch: pytest.MonkeyPatch) -> None:
    # An HTTP error must NEVER render a false "No" (INV-5).
    install_fake_sources(monkeypatch, shortage_raises=True)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["drug_shortage"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert "failed" in (cell["note"] or "")


def test_rems_verified_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, has_rems=False)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["rems"]
    assert cell["status"] == "verified_absent"


def test_rems_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, has_rems=True)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["rems"]
    assert cell["status"] == "populated"
    assert cell["value"] == "Yes"


def test_rems_ambiguous_match_collapses_to_analyst(monkeypatch: pytest.MonkeyPatch) -> None:
    # A name-only fuzzy hit may belong to ANOTHER application of the same
    # ingredient — an ambiguous match never populates "Yes" (INV-5).
    install_fake_sources(monkeypatch, has_rems=True, rems_ambiguous=True)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["rems"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert cell["evidence"]  # the candidate rows ride along for the analyst
    assert "ambiguous" in (cell["note"] or "")


def test_rems_unparseable_index_collapses_to_analyst(monkeypatch: pytest.MonkeyPatch) -> None:
    # Zero TOTAL parsed rows = degraded scrape, not "queried, genuinely absent".
    install_fake_sources(monkeypatch, has_rems=False, rems_index_rows=0)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["rems"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert "no parseable rows" in (cell["note"] or "")


def test_rems_without_identity_terms_collapses(monkeypatch: pytest.MonkeyPatch) -> None:
    # The index cannot be keyed by appl_no alone: with no ingredient and no
    # brand, an empty result is structural — never a verified "No".
    install_fake_sources(monkeypatch, has_rems=False)

    def nameless_products(application_number: str, *, client: object = None) -> OrangeBookRows:
        return OrangeBookRows(
            rows=[
                {
                    "appl_type": "N",
                    "appl_no": APPL_NO,
                    "product_no": "001",
                    "ingredient": "",
                    "trade_name": "ALBUTEROL SULFATE",
                    "dosage_form_route": "AEROSOL, METERED;INHALATION",
                    "strength": "0.09MG/INH",
                }
            ],
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(orange_book, "product_rows", nameless_products)
    monkeypatch.setattr(populator, "_drugsfda_records", lambda q: [])
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["rems"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert "application number alone" in (cell["note"] or "")


def test_shortage_resolved_only_does_not_assert_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    # openFDA retains resolved shortages as history: a resolved-only record set
    # must not lead the "On Shortage List? Y/N" cell with "Yes".
    install_fake_sources(monkeypatch, on_shortage=True, shortage_status="Resolved")
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["drug_shortage"]
    assert cell["status"] == "populated"
    assert not (cell["value"] or "").startswith("Yes")
    assert "Resolved" in cell["value"]
    assert cell["evidence"]


# --------------------------- evidence-only cells ---------------------------
def test_evidence_only_indication(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["indication"]
    assert cell["status"] == "populated"
    assert "bronchospasm" in cell["value"]
    assert cell["evidence"][0]["locator"] == "SPL_abc-def-123#34067-9"


def test_evidence_only_pllr_and_plr(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    assert cells["pllr_format"]["status"] == "populated"
    assert "42228-7" in cells["pllr_format"]["value"]
    assert cells["plr_format"]["status"] == "populated"
    assert "PLR format" in cells["plr_format"]["value"]
    assert cells["plr_format"]["note"]  # low-confidence note present


def test_evidence_only_pregnancy_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["pregnancy_registry"]
    assert cell["status"] == "populated"
    assert "1-800-555-0100" in cell["value"]


def test_evidence_only_epc(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["epc"]
    assert cell["status"] == "populated"
    assert "[EPC]" in cell["value"]


def test_dea_absent_field_collapses_to_analyst(monkeypatch: pytest.MonkeyPatch) -> None:
    # No dea_schedule in the record: do NOT assert "N/A" (absence != non-scheduled).
    install_fake_sources(monkeypatch, dea_schedule=None)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["dea_classification"]
    assert cell["status"] == "analyst_input_required"


def test_dea_populated_when_scheduled(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, dea_schedule="CIV")
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["dea_classification"]
    assert cell["status"] == "populated"
    assert "CIV" in cell["value"]


def test_spl_unresolved_collapses_spl_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, setid=None)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    for cell_id in ("indication", "pllr_format", "plr_format", "pregnancy_registry"):
        assert cells[cell_id]["status"] == "analyst_input_required", cell_id


# --------------------------- evidence-only Requirements ---------------------------
def test_requirements_populated_from_scoped_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["requirements"]
    assert cell["status"] == "populated"
    assert "fasting" in cell["value"]
    assert cell["evidence"][0]["page"] == 4


def test_requirements_refusal_collapses(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    from regwatch.generate.grounded_qa import QAResult

    def _refused(*a, **k):  # type: ignore[no-untyped-def]
        return QAResult(
            answer="",
            citations=[],
            refused=True,
            model_name="stub",
            audit_id=0,
            retrieved=[],
            status="refused",
        )

    monkeypatch.setattr(populator, "ask", _refused)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["requirements"]
    assert cell["status"] == "analyst_input_required"


# --------------------------- Requirements guiding note ---------------------------
_REQ_SPEC = "requirements"
_CELL_KEYS = {"id", "label", "mode", "status", "value", "evidence", "note"}


def _req_ctx() -> _Ctx:
    """Minimal _Ctx with a resolved ingredient so the PSG ask is scoped."""
    return _Ctx(
        rld_name="Metformin",
        application_number_input="NDA020357",
        appl_no="020357",
        application_type="NDA",
        ingredient="metformin hydrochloride",
        normalized_name="metformin hydrochloride",
        now=datetime(2024, 1, 1, tzinfo=UTC),
        user_id=None,
    )


def _req_cell(monkeypatch: pytest.MonkeyPatch, qa: object) -> dict:
    from regwatch.whitepaper.template import spec_by_id

    spec = spec_by_id(_REQ_SPEC)
    assert spec is not None
    monkeypatch.setattr(populator, "ask", lambda *a, **k: qa)
    return _ext_psg_requirements(spec, _req_ctx())


def test_requirements_collapse_carries_guiding_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused collapse keeps status/value (INV-5) and key set UNCHANGED, but the
    note now guides instead of dead-ending: it names the answerable next steps from
    qa.related."""
    from regwatch.generate.grounded_qa import ClarifyOption, QAResult

    qa = QAResult(
        answer="",
        citations=[],
        refused=True,
        model_name="stub",
        audit_id=0,
        retrieved=[],
        status="refused",
        related=[
            ClarifyOption(
                "Recommended bioequivalence (BE) study",
                "What BE study does FDA recommend for metformin hydrochloride?",
                {"normalized_name": "metformin hydrochloride"},
            ),
            ClarifyOption(
                "Dissolution method",
                "What dissolution method does FDA recommend for metformin hydrochloride?",
                {"normalized_name": "metformin hydrochloride"},
            ),
        ],
    )
    cell = _req_cell(monkeypatch, qa)

    # INV-5: collapse stays an analyst cell with no fabricated value.
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    # Cell key set is pinned (no new "guidance" key — guidance rides in note).
    assert set(cell) == _CELL_KEYS
    # The bare dead-end string is replaced by a guiding next step.
    assert "Answerable next steps" in cell["note"]
    assert "Bioequivalence" in cell["note"] or "bioequivalence" in cell["note"]
    # The base INV citation is preserved (nothing weakened).
    assert "INV-9" in cell["note"]


def test_requirements_collapse_note_includes_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the QAResult carries an interpretation (e.g. the clarify path), the
    closest-match text is surfaced in the note."""
    from regwatch.generate.grounded_qa import ClarifyOption, QAResult

    qa = QAResult(
        answer="",
        citations=[],
        refused=False,
        model_name="stub",
        audit_id=0,
        retrieved=[],
        status="clarify",
        interpretation="You're asking about Metformin Hydrochloride.",
        clarify=[
            ClarifyOption(
                "Metformin Hydrochloride - Tablet (Oral)",
                "scoped question",
                {"normalized_name": "metformin hydrochloride"},
            )
        ],
    )
    cell = _req_cell(monkeypatch, qa)

    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert set(cell) == _CELL_KEYS
    assert "Closest matching guidance" in cell["note"]
    assert "Metformin Hydrochloride" in cell["note"]
    assert "Answerable next steps" in cell["note"]


def test_requirements_collapse_note_empty_qa_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An all-empty refused QAResult (no interpretation/related/clarify) must not
    crash and degrades to the base collapse string."""
    from regwatch.generate.grounded_qa import QAResult

    qa = QAResult(
        answer="",
        citations=[],
        refused=True,
        model_name="stub",
        audit_id=0,
        retrieved=[],
        status="refused",
    )
    cell = _req_cell(monkeypatch, qa)

    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert set(cell) == _CELL_KEYS
    assert "INV-9" in cell["note"]
    assert "Answerable next steps" not in cell["note"]
    assert "Closest matching guidance" not in cell["note"]


# --------------------------- manual cells ---------------------------
def test_manual_cells_never_carry_value(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    for cell_id in (
        "rd_center",
        "priority_status",
        "patents",
        "first_to_market",
        "eftf",
        "combination_product",
        "restricted_distribution",
        "usp_monograph",
        "salable_unit",
        "labeling_carveouts",
        "emergency_use",
        "proposed_strategy",
        "in_vivo_be_studies",
        "prepared_by",
    ):
        cell = cells[cell_id]
        assert cell["status"] == "analyst_input_required", cell_id
        assert cell["value"] is None, cell_id


def test_manual_patent_block_surfaces_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["patents"]
    assert cell["evidence"]
    assert cell["evidence"][0]["locator"] == "OBPAT_RE37410"


def test_manual_exclusivity_block_surfaces_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["first_to_market"]
    assert any(ev["locator"] == "OBEXCL_NCE" for ev in cell["evidence"])


def test_be_requirement_field_surfaces_psg_field(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["dissolution_testing"]
    assert cell["status"] == "analyst_input_required"
    assert any("USP apparatus" in (ev.get("snippet") or "") for ev in cell["evidence"])


# --------------------------- BE guidance presence ---------------------------
def test_be_guidance_available_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["be_guidance_available"]
    assert cell["status"] == "populated"
    assert cell["value"] == "Yes"


def test_be_guidance_empty_store_collapses_to_analyst(monkeypatch: pytest.MonkeyPatch) -> None:
    # A2: an EMPTY/unseeded store proves nothing about FDA's PSG catalog — a
    # "No" from it would be an unverified negative (tri-state, INV-5).
    install_fake_sources(monkeypatch, seed_psg=False)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["be_guidance_available"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert "empty/unseeded" in (cell["note"] or "")


def test_be_guidance_absent_when_seeded_store_has_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "No" requires a corpus to be absent FROM: a seeded store with only an
    # unrelated product's PSG verifies absence for this product.
    install_fake_sources(monkeypatch, seed_psg=False)
    _seed_psg_doc()  # ibuprofen tablet — unrelated to albuterol
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["be_guidance_available"]
    assert cell["status"] == "verified_absent"
    assert cell["value"] == "No"


def test_be_guidance_store_failure_collapses_to_analyst(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failed PSG store query must NEVER render a false "No" (INV-5) —
    # mirrors test_shortage_failure_collapses_to_analyst for the local store.
    install_fake_sources(monkeypatch)

    def _raise(ctx: object) -> list[dict]:
        raise RuntimeError("psg store down")

    monkeypatch.setattr(populator, "_matching_psg_docs", _raise)
    result = build_whitepaper(RLD_NAME, APPL_NO)
    cell = _cells(result)["be_guidance_available"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert "failed" in (cell["note"] or "")
    assert any("PSG store lookup failed" in w for w in result["spine"]["warnings"])


# --------------------------- OB failure path ---------------------------
def test_ob_failure_collapses_ob_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, ob_raises=True)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    # OB failed but Drugs@FDA still resolves identity -> OB-backed cells collapse.
    assert cells["product_name"]["status"] == "analyst_input_required"
    assert cells["strengths"]["status"] == "analyst_input_required"


# --------------------------- provenance / structure guards ---------------------------
def test_nda_number_without_confirmation_collapses(monkeypatch: pytest.MonkeyPatch) -> None:
    # A populated auto cell with empty evidence is a contract violation: when
    # neither Orange Book nor Drugs@FDA confirms the number, collapse.
    install_fake_sources(monkeypatch)

    def productless(application_number: str, *, client: object = None) -> OrangeBookRows:
        return OrangeBookRows(
            rows=[{"appl_type": "N", "appl_no": APPL_NO, "ingredient": "ALBUTEROL SULFATE"}],
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(orange_book, "product_rows", productless)
    monkeypatch.setattr(populator, "_drugsfda_records", lambda q: [])
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["nda_number"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None


def test_plr_pllr_collapse_when_no_sections_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    # Valid XML, zero parsed LOINC sections = degraded parse — a confident
    # structural negative from that state would be unverified (INV-5).
    install_fake_sources(monkeypatch)

    def empty_xml(target_setid: str, *, client: object = None) -> SplXmlDocument:
        return SplXmlDocument(
            setid=target_setid,
            xml='<document xmlns="urn:hl7-org:v3"/>',
            source_url="https://example.invalid/spl",
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(dailymed, "fetch_spl_xml", empty_xml)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    for cell_id in ("plr_format", "pllr_format"):
        assert cells[cell_id]["status"] == "analyst_input_required", cell_id
        assert cells[cell_id]["value"] is None, cell_id


_PREGNANCY_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <component><structuredBody>
    <component><section>
      <code code="42228-7"/>
      <title>PREGNANCY</title>
      <text>{text}</text>
    </section></component>
  </structuredBody></component>
</document>"""


def _install_pregnancy_xml(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    def custom_xml(target_setid: str, *, client: object = None) -> SplXmlDocument:
        return SplXmlDocument(
            setid=target_setid,
            xml=_PREGNANCY_XML_TEMPLATE.format(text=text),
            source_url="https://example.invalid/spl",
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(dailymed, "fetch_spl_xml", custom_xml)


def test_pregnancy_registry_matches_newer_tollfree_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1-855/1-844/1-833 are matched by pattern, not an enumerated prefix list.
    install_fake_sources(monkeypatch)
    _install_pregnancy_xml(monkeypatch, "Report exposures by calling 1-855-555-0199.")
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["pregnancy_registry"]
    assert cell["status"] == "populated"
    assert "1-855-555-0199" in cell["value"]


def test_pregnancy_registry_no_marker_collapses_to_analyst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scan is recall-limited: a miss surfaces the subsection for the
    # analyst instead of populating a generated negative (INV-5).
    install_fake_sources(monkeypatch)
    _install_pregnancy_xml(monkeypatch, "Advise patients of the potential risk to a fetus.")
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["pregnancy_registry"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert any("potential risk" in (ev.get("snippet") or "") for ev in cell["evidence"])


# --------------------------- cross-type application guards ---------------------------
_ANDA_ROW = {
    "appl_type": "A",
    "appl_no": APPL_NO,
    "product_no": "003",
    "ingredient": "ALBUTEROL SULFATE",
    "trade_name": "ALBUTEROL SULFATE",
    "dosage_form_route": "AEROSOL, METERED;INHALATION",
    "strength": "0.108MG/INH",
}


def _mixed_type_products(application_number: str, *, client: object = None) -> OrangeBookRows:
    nda_row = {
        "appl_type": "N",
        "appl_no": APPL_NO,
        "product_no": "001",
        "ingredient": "ALBUTEROL SULFATE",
        "trade_name": "PROVENTIL HFA",
        "dosage_form_route": "AEROSOL, METERED;INHALATION",
        "strength": "0.09MG/INH",
        "rld": "Yes",
        "rs": "Yes",
    }
    return OrangeBookRows(rows=[nda_row, dict(_ANDA_ROW)], fetched_at=datetime.now(UTC))


def test_bare_digits_spanning_types_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # NDA and ANDA rows sharing the digits are DIFFERENT applications: bare
    # digits cannot pick one, so the spine 422s listing what WAS found.
    install_fake_sources(monkeypatch)
    monkeypatch.setattr(orange_book, "product_rows", _mixed_type_products)
    with pytest.raises(SpineResolutionError) as exc:
        build_whitepaper(RLD_NAME, APPL_NO)
    assert "NDA 020503" in exc.value.detail
    assert "ANDA 020503" in exc.value.detail


def test_prefixed_input_filters_other_type_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    # With an explicit prefix the other type's rows are dropped (never blended)
    # and the drop is surfaced as a spine warning.
    install_fake_sources(monkeypatch)
    monkeypatch.setattr(orange_book, "product_rows", _mixed_type_products)
    result = build_whitepaper(RLD_NAME, "NDA 020503")
    assert result["spine"]["product_numbers"] == ["001"]
    assert any("different application type" in w for w in result["spine"]["warnings"])


# --------------------------- priority status evidence ---------------------------
def test_priority_status_carries_patent_and_exclusivity_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The schema pins Priority Status evidence to OB patent + exclusivity rows.
    install_fake_sources(monkeypatch)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["priority_status"]
    assert cell["status"] == "analyst_input_required"
    locators = [ev["locator"] for ev in cell["evidence"]]
    assert "OBPAT_RE37410" in locators
    assert "OBEXCL_NCE" in locators


# --------------------------- INV-8 structured-citation guard ---------------------------
def test_enforce_structured_citation_collapse() -> None:
    from regwatch.whitepaper.template import spec_by_id

    spec = spec_by_id("product_name")
    assert spec is not None
    cell = _cell(
        spec,
        "populated",
        "ALBUTEROL SULFATE",
        [
            {
                "source": "Orange Book",
                "locator": "OB_020503/999",
                "source_url": None,
                "fetched_at": None,
                "page": None,
                "section": None,
                "snippet": None,
            }
        ],
        None,
    )
    collapsed = _enforce_structured_citations(cell, known={"OB_020503/001"})
    assert collapsed["status"] == "analyst_input_required"
    assert collapsed["value"] is None
    assert "INV-8" in collapsed["note"]


def test_enforce_structured_citation_keeps_backed() -> None:
    from regwatch.whitepaper.template import spec_by_id

    spec = spec_by_id("product_name")
    assert spec is not None
    cell = _cell(
        spec,
        "populated",
        "ALBUTEROL SULFATE",
        [
            {
                "source": "Orange Book",
                "locator": "OB_020503/001",
                "source_url": None,
                "fetched_at": None,
                "page": None,
                "section": None,
                "snippet": None,
            }
        ],
        None,
    )
    kept = _enforce_structured_citations(cell, known={"OB_020503/001"})
    assert kept["status"] == "populated"


# --------------------------- A1: REMS identity confirmation ---------------------------
def _rems_record(
    identifiers: dict[str, str] | None = None, raw: dict[str, str] | None = None
) -> SourceRecord:
    return SourceRecord(
        source=SourceKind.REMS,
        title="REMS: SOMEDRUG",
        source_url="https://www.accessdata.fda.gov/scripts/cder/rems/index.cfm",
        identifiers=identifiers or {},
        fields={},
        raw=raw or {},
    )


def test_rems_match_rejects_other_type_sharing_digits() -> None:
    # endswith(bare digits) was prefix-blind: ANDA020503 confirmed NDA020503.
    rec = _rems_record(identifiers={"application_number": "ANDA020503"})
    assert not _rems_record_matches_application(rec, "NDA020503")
    assert _rems_record_matches_application(rec, "ANDA020503")


def test_rems_match_ignores_digit_collisions_in_unrelated_raw_values() -> None:
    # The unanchored substring scan matched digits inside URLs/dates/free text.
    rec = _rems_record(
        raw={
            "info_url": "https://accessdata.fda.gov/rems?program=1020503",
            "updated": "2020-05-03",
            "drug_name": "SOMEDRUG",
        }
    )
    assert not _rems_record_matches_application(rec, "NDA020503")


def test_rems_match_accepts_typed_free_text_number() -> None:
    rec = _rems_record(raw={"drug_name": "SOMEDRUG", "application_number": "NDA #020503"})
    assert _rems_record_matches_application(rec, "NDA020503")


def test_rems_match_bare_digit_column_never_confirms() -> None:
    # A bare-digit value cannot name an application TYPE; it stays ambiguous.
    rec = _rems_record(raw={"application_number": "020503"})
    assert not _rems_record_matches_application(rec, "NDA020503")


def test_rems_other_type_record_collapses_to_analyst(monkeypatch: pytest.MonkeyPatch) -> None:
    # End to end: a digit-colliding other-type REMS row must never render "Yes".
    install_fake_sources(monkeypatch)

    def other_type_rems(query: SourceQuery) -> tuple[list[SourceRecord], int]:
        return [_rems_record(identifiers={"application_number": f"ANDA{APPL_NO}"})], 47

    monkeypatch.setattr(populator, "_rems_search", other_type_rems)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["rems"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert cell["evidence"]  # candidates ride along for the analyst


# --------------------------- A3: PSG dosage-form scoping ---------------------------
def test_be_guidance_never_yes_from_another_forms_psg(monkeypatch: pytest.MonkeyPatch) -> None:
    # A name-only PSG match for a DIFFERENT dosage form must not populate "Yes"
    # citing the other form's guidance — and must not read as "No" either.
    install_fake_sources(monkeypatch, seed_psg=False)
    _seed_psg_doc(
        active_ingredient="Albuterol Sulfate",
        normalized_name="albuterol sulfate",
        dosage_form="Tablet, Extended Release",
        route="Oral",
        content_hash="hash-tablet",
    )
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["be_guidance_available"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert "Tablet, Extended Release" in (cell["note"] or "")
    assert cell["evidence"]  # the other-form PSG is surfaced for the analyst


def test_be_guidance_yes_for_compatible_form_name_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sources(monkeypatch, seed_psg=False)
    _seed_psg_doc(
        active_ingredient="Albuterol Sulfate",
        normalized_name="albuterol sulfate",
        dosage_form="Aerosol, Metered",
        route="Inhalation",
        content_hash="hash-aerosol",
    )
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["be_guidance_available"]
    assert cell["status"] == "populated"
    assert cell["value"] == "Yes"


def test_be_requirement_prefers_exact_form_doc_over_higher_version_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # version_id orders versions WITHIN one document; the old global
    # ORDER BY version_id let an unrelated doc's newer row win.
    install_fake_sources(monkeypatch)  # seeds the exact-form Aerosol, Metered PSG
    other_id = _seed_psg_doc(
        active_ingredient="Albuterol Sulfate",
        normalized_name="albuterol sulfate",
        dosage_form="Aerosol",  # compatible (substring) but NOT exact
        route="Inhalation",
        content_hash="hash-aerosol-plain",
    )
    _seed_be_requirement(other_id, dissolution="DOC2 dissolution profile")
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["dissolution_testing"]
    snippets = [ev.get("snippet") or "" for ev in cell["evidence"]]
    assert any("USP apparatus" in s for s in snippets)  # the exact-form doc's field
    assert not any("DOC2 dissolution" in s for s in snippets)


def test_be_requirement_collapses_when_multiple_docs_and_no_exact_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sources(monkeypatch)
    other_id = _seed_psg_doc(
        active_ingredient="Albuterol Sulfate",
        normalized_name="albuterol sulfate",
        dosage_form="Aerosol, Metered",  # a SECOND exact-form doc -> ambiguous
        route="Inhalation",
        content_hash="hash-aerosol-2",
    )
    _seed_be_requirement(other_id, dissolution="DOC2 dissolution profile")
    result = build_whitepaper(RLD_NAME, APPL_NO)
    cell = _cells(result)["dissolution_testing"]
    snippets = [ev.get("snippet") or "" for ev in cell["evidence"]]
    # No single applicable document -> no study field surfaced, never blended;
    # all candidate PSGs ride along as evidence.
    assert not any("USP apparatus" in s for s in snippets)
    assert not any("DOC2 dissolution" in s for s in snippets)
    assert sum(1 for ev in cell["evidence"] if ev["source"] == "PSG store") >= 2
    assert any("not blended" in w for w in result["spine"]["warnings"])


def test_form_compatible_requires_exact_normalized_equality() -> None:
    # Bidirectional containment let an IR product treat the ER form's PSG as
    # compatible ("tablet" ⊂ "tablet extended release") and vice versa —
    # release types are distinct PSGs with different BE recommendations.
    assert not _form_compatible("Tablet, Extended Release", {"tablet"})
    assert not _form_compatible("Tablet", {"tablet extended release"})
    assert _form_compatible("TABLET", {"tablet"})
    assert _form_compatible("Aerosol, Metered", {"aerosol metered"})
    assert not _form_compatible("Aerosol", {"aerosol metered"})
    assert not _form_compatible(None, {"tablet"})  # no recorded form -> analyst path


def test_be_guidance_never_yes_from_release_type_variant_psg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Containment let an IR TABLET;ORAL product cite — and pull study fields
    # from — the same molecule's "Tablet, Extended Release" PSG (INV-1/5).
    install_fake_sources(monkeypatch, seed_psg=False)

    def tablet_products(application_number: str, *, client: object = None) -> OrangeBookRows:
        return OrangeBookRows(
            rows=[
                {
                    "appl_type": "N",
                    "appl_no": APPL_NO,
                    "product_no": "001",
                    "ingredient": "ALBUTEROL SULFATE",
                    "trade_name": "PROVENTIL",
                    "dosage_form_route": "TABLET;ORAL",
                    "strength": "2MG",
                    "rld": "Yes",
                    "rs": "Yes",
                }
            ],
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(orange_book, "product_rows", tablet_products)
    er_id = _seed_psg_doc(
        active_ingredient="Albuterol Sulfate",
        normalized_name="albuterol sulfate",
        dosage_form="Tablet, Extended Release",
        route="Oral",
        content_hash="hash-er",
    )
    _seed_be_requirement(er_id, dissolution="ER dissolution profile")
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    cell = cells["be_guidance_available"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert cell["evidence"]  # the ER PSG rides along for the analyst
    assert "Tablet, Extended Release" in (cell["note"] or "")
    # The ER form's study fields never surface as this product's.
    snippets = [ev.get("snippet") or "" for ev in cells["dissolution_testing"]["evidence"]]
    assert not any("ER dissolution" in s for s in snippets)


def test_be_guidance_unverifiable_when_application_form_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Orange Book yields no product rows (identity resolved from Drugs@FDA
    # alone): name-matched PSGs used to be kept with NO form check, so a PSG
    # whose form was never verified could be cited as this product's "Yes".
    install_fake_sources(monkeypatch, seed_psg=False)

    def empty_rows(application_number: str, *, client: object = None) -> OrangeBookRows:
        return OrangeBookRows(rows=[], fetched_at=datetime.now(UTC))

    monkeypatch.setattr(orange_book, "product_rows", empty_rows)
    monkeypatch.setattr(orange_book, "patent_rows", empty_rows)
    monkeypatch.setattr(orange_book, "exclusivity_rows", empty_rows)
    _seed_psg_doc(
        active_ingredient="Albuterol Sulfate",
        normalized_name="albuterol sulfate",
        dosage_form="Aerosol, Metered",
        route="Inhalation",
        content_hash="hash-unverified",
    )
    result = build_whitepaper(RLD_NAME, APPL_NO)
    cell = _cells(result)["be_guidance_available"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None
    assert cell["evidence"]  # the unverifiable PSG is surfaced for the analyst
    assert "could not be established" in (cell["note"] or "")
    assert any("could not be form-verified" in w for w in result["spine"]["warnings"])


# --------------------------- A5/C1: resolved number on post-resolution queries ----------
def test_post_resolution_queries_use_resolved_prefixed_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bare-digit input resolves to NDA; the raw input's candidate expansion ORs
    # NDA/ANDA/BLA, so post-resolution queries must pass the PREFIXED number.
    install_fake_sources(monkeypatch)
    seen: dict[str, str | None] = {}

    def capture_shortage(query: SourceQuery) -> list[SourceRecord]:
        seen["shortage"] = query.application_number
        return []

    def capture_ndc(query: SourceQuery) -> list[SourceRecord]:
        seen["ndc"] = query.application_number
        return []

    def capture_rems(query: SourceQuery) -> tuple[list[SourceRecord], int]:
        seen["rems"] = query.application_number
        return [], 47

    def capture_resolve(
        application_number: str, *, prefer_titles: object = (), client: object = None
    ) -> SetidResolution | None:
        seen["dailymed"] = application_number
        return None

    monkeypatch.setattr(populator, "_shortage_records", capture_shortage)
    monkeypatch.setattr(populator, "_ndc_records", capture_ndc)
    monkeypatch.setattr(populator, "_rems_search", capture_rems)
    monkeypatch.setattr(dailymed, "resolve_setid", capture_resolve)
    build_whitepaper(RLD_NAME, APPL_NO)  # bare digits in
    assert seen["shortage"] == f"NDA{APPL_NO}"
    assert seen["ndc"] == f"NDA{APPL_NO}"
    assert seen["rems"] == f"NDA{APPL_NO}"
    assert seen["dailymed"] == f"NDA{APPL_NO}"


# --------------------------- A6: Drugs@FDA never overrides / never blends ----------
def _drugsfda_rec(application_number: str, sponsor: str) -> SourceRecord:
    return SourceRecord(
        source=SourceKind.DRUGSFDA,
        title=f"Drugs@FDA: {application_number}",
        source_url="https://open.fda.gov/apis/drug/drugsfda/",
        identifiers={"application_number": application_number},
        fields={"sponsor_name": sponsor, "products": []},
        raw={},
    )


def test_drugsfda_type_never_overrides_orange_book_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OB confirms NDA; a digit-colliding ANDA Drugs@FDA record previously
    # overrode the resolved type AND leaked its sponsor into cells.
    install_fake_sources(monkeypatch)
    monkeypatch.setattr(
        populator,
        "_drugsfda_records",
        lambda q: [_drugsfda_rec(f"ANDA{APPL_NO}", "OTHER GENERICS CORP")],
    )
    result = build_whitepaper(RLD_NAME, APPL_NO)
    assert result["spine"]["application_type"] == "NDA"
    assert any("Dropped 1 Drugs@FDA record" in w for w in result["spine"]["warnings"])
    holder = _cells(result)["nda_holder"]
    # The other application's sponsor never populates this cell; the OB
    # applicant rows back it instead.
    assert "OTHER GENERICS CORP" not in (holder["value"] or "")
    assert "MERCK" in (holder["value"] or "")


def test_drugsfda_records_filtered_to_resolved_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sources(monkeypatch)
    monkeypatch.setattr(
        populator,
        "_drugsfda_records",
        lambda q: [
            _drugsfda_rec(f"NDA{APPL_NO}", "MERCK SHARP DOHME CORP"),
            _drugsfda_rec(f"ANDA{APPL_NO}", "OTHER GENERICS CORP"),
        ],
    )
    result = build_whitepaper(RLD_NAME, APPL_NO)
    cells = _cells(result)
    locators = [ev["locator"] for ev in cells["nda_holder"]["evidence"]]
    assert f"ANDA{APPL_NO}" not in locators
    assert any("Dropped 1 Drugs@FDA record" in w for w in result["spine"]["warnings"])


def test_bare_digits_ob_down_drugsfda_both_types_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With OB unavailable, bare digits + Drugs@FDA records spanning both types
    # is the same ambiguity as the OB-based guard -> 422, never a blend.
    install_fake_sources(monkeypatch, ob_raises=True)
    monkeypatch.setattr(
        populator,
        "_drugsfda_records",
        lambda q: [
            _drugsfda_rec(f"NDA{APPL_NO}", "MERCK SHARP DOHME CORP"),
            _drugsfda_rec(f"ANDA{APPL_NO}", "OTHER GENERICS CORP"),
        ],
    )
    with pytest.raises(SpineResolutionError) as exc:
        build_whitepaper(RLD_NAME, APPL_NO)
    assert f"NDA {APPL_NO}" in exc.value.detail
    assert f"ANDA {APPL_NO}" in exc.value.detail


# --------------------------- A7: RLD-name verification floor ---------------------------
@pytest.mark.parametrize("name", ["a", "  ", "ab"])
def test_too_short_rld_name_raises(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    # Bidirectional substring waved 1-char and whitespace names through.
    install_fake_sources(monkeypatch)
    with pytest.raises(SpineResolutionError) as exc:
        build_whitepaper(name, APPL_NO)
    assert "too short" in exc.value.detail


def test_short_substring_no_longer_verifies_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # "pro" is a 3-char substring of PROVENTIL HFA — containment below 4 chars
    # proves nothing, so the spine refuses with a mismatch (not a build).
    install_fake_sources(monkeypatch)
    with pytest.raises(SpineResolutionError) as exc:
        build_whitepaper("pro", APPL_NO)
    assert "does not match" in exc.value.detail


def test_name_matches_containment_and_equality_rules() -> None:
    assert _name_matches("ABC", ["abc"])  # exact case-folded equality always passes
    assert _name_matches("prov", ["PROVENTIL HFA"])  # 4-char containment counts
    assert not _name_matches("hfa", ["PROVENTIL HFA"])  # 3-char containment does not
    with pytest.raises(SpineResolutionError):
        _name_matches("ab", ["PROVENTIL HFA"])


# --------------------------- A8: empty-value choke point ---------------------------
def test_populated_rejects_empty_and_whitespace_values() -> None:
    from regwatch.whitepaper.template import spec_by_id

    spec = spec_by_id("product_name")
    assert spec is not None
    for value in ("", "   ", "\n\t"):
        cell = _populated(spec, value, [], note="from a real query")
        assert cell["status"] == "analyst_input_required"
        assert cell["value"] is None
        assert "source returned an empty value" in (cell["note"] or "")
    kept = _populated(spec, "ALBUTEROL", [])
    assert kept["status"] == "populated"


def test_whitespace_spl_section_collapses_to_analyst(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    _install_pregnancy_xml(monkeypatch, "Call the registry at 1-800-555-0100.")

    class _Blank:
        text = "   "
        title = "INDICATIONS"
        source_url = "https://example.invalid/spl"
        fetched_at = None

    def blank_sections(*args: object, **kwargs: object) -> dict:
        return {"34067-9": _Blank()}

    monkeypatch.setattr(dailymed, "parse_spl_sections", blank_sections)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["indication"]
    assert cell["status"] == "analyst_input_required"
    assert cell["value"] is None


# --------------------------- A10: per-rowset freshness on evidence ---------------------------
def test_patent_evidence_carries_patent_rowset_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # patents.txt is fetched separately from products.txt; its evidence must
    # carry ITS fetch time, not the products rowset's.
    install_fake_sources(monkeypatch)
    patents_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    def fake_patents(application_number: str, *, client: object = None) -> OrangeBookRows:
        return OrangeBookRows(
            rows=[
                {
                    "appl_type": "N",
                    "appl_no": APPL_NO,
                    "product_no": "001",
                    "patent_no": "RE37410",
                    "patent_expire_date": "Aug 22, 2017",
                }
            ],
            fetched_at=patents_at,
        )

    monkeypatch.setattr(orange_book, "patent_rows", fake_patents)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    patent_ev = cells["patents"]["evidence"][0]
    assert patent_ev["fetched_at"] == patents_at.isoformat()
    product_ev = cells["product_name"]["evidence"][0]
    assert product_ev["fetched_at"] != patents_at.isoformat()


# --------------------------- A11: one REMS fetch per build ---------------------------
def test_rems_index_fetched_once_per_build(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, has_rems=True)
    fake = populator._rems_search
    calls: list[SourceQuery] = []

    def counting(query: SourceQuery) -> tuple[list[SourceRecord], int]:
        calls.append(query)
        return fake(query)

    monkeypatch.setattr(populator, "_rems_search", counting)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    # Both REMS-backed cells consumed evidence...
    assert cells["rems"]["status"] == "populated"
    assert cells["restricted_distribution"]["evidence"]
    # ...from ONE index fetch+parse, queried with brand AND ingredient terms.
    assert len(calls) == 1
    assert calls[0].brand_name == "PROVENTIL HFA"
    assert calls[0].active_ingredient == "ALBUTEROL SULFATE"


def test_restricted_distribution_inherits_parse_sanity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Zero TOTAL parsed rows = degraded scrape; the manual cell's note must say
    # so instead of implying "queried and empty".
    install_fake_sources(monkeypatch, has_rems=False, rems_index_rows=0)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["restricted_distribution"]
    assert cell["status"] == "analyst_input_required"
    assert "no parseable rows" in (cell["note"] or "")
