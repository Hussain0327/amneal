"""White-Paper compliance invariants.

The populator lives on the compliance line: it surfaces, organizes, and cites;
it never renders regulatory judgment (INV-3) and never asserts an unverified
fact (INV-5). These tests are structural — they walk the registry and the built
output rather than trusting any single extractor.
"""

from __future__ import annotations

import pytest

from regwatch.whitepaper.populator import build_whitepaper
from regwatch.whitepaper.template import CELL_SPECS, CellMode
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources

pytestmark = pytest.mark.invariants


def _cells(result: dict) -> dict[str, dict]:
    return {c["id"]: c for s in result["sections"] for c in s["cells"]}


def test_registry_mirrors_the_four_schema_sections() -> None:
    sections = {spec.section for spec in CELL_SPECS}
    assert sections == {
        "Proposed Generic Product",
        "Reference Listed Drug Product",
        "Product Specific Bioequivalence Recommendation Guidance",
        "Action Items",
    }
    # Every cell id is unique and every spec has a label + extractor.
    ids = [spec.id for spec in CELL_SPECS]
    assert len(ids) == len(set(ids))
    assert all(spec.label and spec.extractor for spec in CELL_SPECS)


def test_manual_cells_never_carry_a_generated_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # INV-3: walk the registry; every manual cell, however populated, must end as
    # analyst_input_required with value=None.
    install_fake_sources(monkeypatch, on_shortage=True, has_rems=True, dea_schedule="CIV")
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    for spec in CELL_SPECS:
        if spec.mode is CellMode.MANUAL:
            cell = cells[spec.id]
            assert cell["status"] == "analyst_input_required", spec.id
            assert cell["value"] is None, spec.id


def test_every_populated_cell_carries_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    # INV-5: a populated value must trace to a fetched source row.
    install_fake_sources(monkeypatch)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    for cell in cells.values():
        if cell["status"] == "populated":
            assert cell["evidence"], cell["id"]
            for ev in cell["evidence"]:
                assert ev["source"], cell["id"]
                assert ev["locator"], cell["id"]


def test_yes_no_cells_are_tristate_not_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failed shortage query must NOT render "No" — it collapses to analyst input.
    install_fake_sources(monkeypatch, shortage_raises=True)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    assert cells["drug_shortage"]["status"] == "analyst_input_required"
    assert cells["drug_shortage"]["value"] is None


def test_verified_absent_only_on_successful_empty_query(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch, on_shortage=False, has_rems=False)
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    for cell_id in ("drug_shortage", "rems"):
        cell = cells[cell_id]
        assert cell["status"] == "verified_absent", cell_id
        assert cell["value"] == "No", cell_id
        # The query that produced the "No" is recorded as evidence.
        assert cell["evidence"], cell_id


def test_populated_cells_never_carry_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    # The empty-value rule: a populated cell renders a FACT; a blank value is
    # not a fact and must have collapsed to analyst input at the choke point.
    install_fake_sources(monkeypatch, on_shortage=True, has_rems=True, dea_schedule="CIV")
    cells = _cells(build_whitepaper(RLD_NAME, APPL_NO))
    for cell in cells.values():
        if cell["status"] == "populated":
            assert cell["value"] is not None, cell["id"]
            assert cell["value"].strip(), cell["id"]


def test_no_draft_or_submit_extractor_or_label() -> None:
    # The vocabulary stays on the surfacing side of the line (INV-3).
    banned = ("draft", "submit", "file anda", "recommend filing")
    for spec in CELL_SPECS:
        haystack = f"{spec.id} {spec.extractor}".lower()
        assert not any(b in haystack for b in banned), spec.id


def test_all_modes_present_in_registry() -> None:
    modes = {spec.mode for spec in CELL_SPECS}
    assert modes == {CellMode.AUTO, CellMode.EVIDENCE_ONLY, CellMode.MANUAL}
