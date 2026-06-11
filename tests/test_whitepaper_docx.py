"""White-Paper docx writer — synthetic template fill + from-scratch fallback."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from docx import Document

from regwatch.whitepaper.docx_writer import docx_media_type, write_whitepaper_docx
from regwatch.whitepaper.populator import build_whitepaper
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources


def _synthetic_template(path: Path) -> None:
    """A tiny structurally-equivalent template (label | value 2-col tables)."""
    doc = Document()
    doc.add_heading("CRA White Paper", level=0)
    sec1 = doc.add_table(rows=0, cols=2)
    for label in ("Product Name", "Strengths", "Reference Listed Drug (RLD)"):
        row = sec1.add_row().cells
        row[0].text = label
        row[1].text = ""
    checks = doc.add_table(rows=0, cols=2)
    for label in ("REMS", "Drug Shortages", "DEA Classification"):
        row = checks.add_row().cells
        row[0].text = label
        row[1].text = "Yes\t\tNo"
    doc.save(str(path))


def _build(monkeypatch: pytest.MonkeyPatch) -> dict:
    install_fake_sources(monkeypatch)
    return build_whitepaper(RLD_NAME, APPL_NO)


def test_from_scratch_when_template_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _build(monkeypatch)
    data = write_whitepaper_docx(result, template_path=tmp_path / "does-not-exist.docx")
    assert data[:2] == b"PK"  # a real .docx (zip) was produced
    doc = Document(BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "CRA White Paper" in text
    # All four section headings + the provenance appendix render.
    headings = {p.text for p in doc.paragraphs}
    assert "Provenance appendix" in headings
    # The populated product name and a structured locator are present somewhere.
    all_cells = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert "ALBUTEROL SULFATE" in all_cells
    assert "OB_020503/001" in all_cells


def test_fills_real_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    template = tmp_path / "template.docx"
    _synthetic_template(template)
    result = _build(monkeypatch)
    data = write_whitepaper_docx(result, template_path=template)
    doc = Document(BytesIO(data))
    cell_text = [c.text for t in doc.tables for r in t.rows for c in r.cells]
    joined = "\n".join(cell_text)
    # Value cells filled in place.
    assert any("ALBUTEROL SULFATE" in c for c in cell_text)
    # Checkbox rows get an appended marker rather than overwriting the Yes/No text.
    assert "->" in joined
    assert "Analyst input required" in joined  # DEA collapses -> marker
    # Provenance appendix present.
    assert "Provenance appendix" in "\n".join(p.text for p in doc.paragraphs)


def _real_shaped_template(path: Path) -> None:
    """Mirrors the real template's hard rows: the merged Priority-Status
    checkbox block, parenthetical label suffixes, the merged "Dosage Form;
    Route" row, and a duplicated label row."""
    doc = Document()
    table = doc.add_table(rows=0, cols=2)
    for label in (
        "Product Name",
        "Dosage Form; Route",
        "Established Pharmacologic Class (A scientifically valid classification system "
        "used by the FDA in drug labeling)",
        "Pregnancy Registry Contact Detail (if required)",
        "Salable Unit (from Sales & Marketing)",
    ):
        row = table.add_row().cells
        row[0].text = label
        row[1].text = ""
    # The real template repeats the Combination Product label (Yes/No row +
    # the Type 1-9 list row).
    for prefill in ("No / Yes", "Type 1 / Type 2 / Type 3"):
        row = table.add_row().cells
        row[0].text = "Combination Product"
        row[1].text = prefill
    # Merged Priority Status block: one value cell carrying the whole
    # Patents / PI-PIV / eFTF / Drug-Shortages checkbox section.
    row = table.add_row().cells
    row[0].text = "Priority Status"
    block = row[1]
    block.text = "Patents (OB review):"
    block.add_paragraph("No Relevant Patents / PI / PII / PIII / PIV / Section viii")
    block.add_paragraph("If PI - Eligible for First to Market? Yes/No")
    block.add_paragraph("If PIV - Eligible for eFTF (PIV Website review)? Yes/No")
    block.add_paragraph("Drug Shortages:")
    block.add_paragraph("On Drug Shortage List? Yes/No")
    doc.save(str(path))


def _table_cell_texts(data: bytes) -> list[str]:
    doc = Document(BytesIO(data))
    return [c.text for t in doc.tables for r in t.rows for c in r.cells]


def test_priority_block_preserved_and_marked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    template = tmp_path / "real-shaped.docx"
    _real_shaped_template(template)
    result = _build(monkeypatch)
    data = write_whitepaper_docx(result, template_path=template)
    doc = Document(BytesIO(data))
    block_cell = next(
        r.cells[-1]
        for t in doc.tables
        for r in t.rows
        if r.cells and r.cells[0].text.strip() == "Priority Status"
    )
    # The template's own checkbox lines survive...
    assert "If PIV - Eligible for eFTF (PIV Website review)? Yes/No" in block_cell.text
    assert "On Drug Shortage List? Yes/No" in block_cell.text
    # ...and the block members ride in as appended markers (drug_shortage is
    # verified_absent in the stub -> "No"; the manual cells -> analyst marker).
    assert "Drug Shortages: On Shortage List? Y/N  ->  No" in block_cell.text
    assert "Analyst input required" in block_cell.text


def test_prefix_matched_labels_fill_their_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    template = tmp_path / "real-shaped.docx"
    _real_shaped_template(template)
    result = _build(monkeypatch)
    data = write_whitepaper_docx(result, template_path=template)
    doc = Document(BytesIO(data))
    by_label = {
        r.cells[0].text.strip(): r.cells[-1].text for t in doc.tables for r in t.rows if r.cells
    }
    epc_row = next(v for k, v in by_label.items() if k.startswith("Established Pharmacologic"))
    assert "[EPC]" in epc_row
    pregnancy_row = next(v for k, v in by_label.items() if k.startswith("Pregnancy Registry"))
    assert "1-800-555-0100" in pregnancy_row
    salable_row = next(v for k, v in by_label.items() if k.startswith("Salable Unit"))
    assert "Analyst input required" in salable_row
    # Route rides into the merged "Dosage Form; Route" row.
    dosage_row = by_label["Dosage Form; Route"]
    assert "AEROSOL, METERED" in dosage_row
    assert "Route: INHALATION" in dosage_row


def test_duplicate_label_rows_filled_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    template = tmp_path / "real-shaped.docx"
    _real_shaped_template(template)
    result = _build(monkeypatch)
    data = write_whitepaper_docx(result, template_path=template)
    doc = Document(BytesIO(data))
    combo_rows = [
        r.cells[-1].text
        for t in doc.tables
        for r in t.rows
        if r.cells and r.cells[0].text.strip() == "Combination Product"
    ]
    assert len(combo_rows) == 2
    marked = [text for text in combo_rows if "->" in text]
    assert len(marked) == 1  # the marker lands once, in the first matching row
    assert "Type 1 / Type 2 / Type 3" in combo_rows[1]
    assert "->" not in combo_rows[1]


def test_media_type() -> None:
    assert "wordprocessingml.document" in docx_media_type()


def test_provenance_appendix_lists_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _build(monkeypatch)
    data = write_whitepaper_docx(result, template_path=tmp_path / "missing.docx")
    doc = Document(BytesIO(data))
    # Find the provenance table (4 columns: Cell, Source, Locator, Fetched at).
    prov = [t for t in doc.tables if t.rows and t.rows[0].cells[0].text == "Cell"]
    assert prov, "provenance table not found"
    rows_text = "\n".join(c.text for t in prov for r in t.rows for c in r.cells)
    assert "OB_020503/001" in rows_text
    assert "Orange Book" in rows_text
