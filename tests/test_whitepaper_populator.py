"""White-Paper populator — spine resolution, every mode, tri-state, INV-8."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from regwatch.sources import dailymed, orange_book
from regwatch.sources.dailymed import SplXmlDocument
from regwatch.sources.orange_book import OrangeBookRows
from regwatch.whitepaper import populator
from regwatch.whitepaper.populator import (
    SpineResolutionError,
    _cell,
    _enforce_structured_citations,
    build_whitepaper,
)
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources


def _cells(result: dict) -> dict[str, dict]:
    return {c["id"]: c for s in result["sections"] for c in s["cells"]}


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


def test_be_guidance_absent_when_no_psg(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, seed_psg=False)
    cell = _cells(build_whitepaper(RLD_NAME, APPL_NO))["be_guidance_available"]
    assert cell["status"] == "verified_absent"


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
