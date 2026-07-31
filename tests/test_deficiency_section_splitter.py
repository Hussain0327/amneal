"""Section splitting from structured block/table JSON: headings, data-row rejection, stitching.

Ported from upstream tests/unit/test_section_splitter.py (DefPredict). Import rewritten
onto the vendored modules; logic and assertions otherwise unchanged.

Deviations beyond the import-rewrite map:
  * The sample-PDF test reads REGWATCH_DEFICIENCY_SAMPLE_PDF instead of the upstream
    SAMPLE_DATA_DIR-relative path, and skips when unset (no PDF fixtures are vendored).
  * The `offline` fixture patches parse.ocr.get_settings the same way upstream did, but
    forces the regwatch OCR seam's fields (deficiency_ocr_invocations_url /
    deficiency_ocr_token) empty instead of upstream's databricks_host / databricks_token
    -- parse/ocr.py now wires OCR via a full invocations URL + bearer token (see its
    module docstring), not a Databricks host + endpoint name.
"""

import os
import re

import pytest

from regwatch.deficiency.parse.section_splitter import (
    _geometry_headings,
    _stitch_cross_page_tables,
    split_document,
)

SAMPLE_PDF = os.environ.get("REGWATCH_DEFICIENCY_SAMPLE_PDF", "")
skip_if_no_sample = pytest.mark.skipif(
    not SAMPLE_PDF or not os.path.exists(SAMPLE_PDF),
    reason="REGWATCH_DEFICIENCY_SAMPLE_PDF not set",
)


@pytest.fixture
def offline(monkeypatch):
    """Force the no-Databricks OCR fallback so the test is fast and deterministic."""
    from config.settings import Settings

    import regwatch.deficiency.parse.ocr as ocrmod

    monkeypatch.setattr(
        ocrmod,
        "get_settings",
        lambda: Settings(deficiency_ocr_invocations_url="", deficiency_ocr_token=""),
    )


def _block(text):
    return {"text": text, "page": 1, "reading_order": 0, "lines": []}


def _grid(page, y0, y1, headers, rows, title=""):
    return {
        "kind": "grid",
        "title": title,
        "headers": headers,
        "rows": rows,
        "pairs": [],
        "page": page,
        "bbox": (72, y0, 540, y1),
        "n_cols": len(headers),
        "n_rows": len(rows) + 1,
        "source_pages": [page],
        "continues_from": False,
        "continues_to": False,
    }


@skip_if_no_sample
def test_split_matches_toc_not_data_rows(offline):
    from regwatch.deficiency.parse.pdf import extract_pdf

    sections = split_document(extract_pdf(SAMPLE_PDF))
    assert 12 <= len(sections) <= 30  # the flat-text splitter produced 95
    headings = [s["heading"] for s in sections]
    assert any("Linearity" in h for h in headings)
    assert any("Conclusions" in h for h in headings)
    for h in headings:  # no data-row false headings
        match = re.match(r"^(\d+(?:\.\d+)*)\s", h)
        if match:
            assert all(int(p) <= 50 for p in match.group(1).split("."))


def test_geometry_headings_reject_data_rows():
    blocks = [
        _block("1.4.2 Linearity"),
        _block("6920.93 Intercept"),
        _block("281.39 CAS Number"),
        _block("1.5 Conclusions"),
    ]
    assert [num for _, num, _ in _geometry_headings(blocks)] == ["1.4.2", "1.5"]


def test_geometry_headings_accept_trailing_dot():
    blocks = [_block("1. Purpose"), _block("2. Scope"), _block("3.1. Procedure Detail")]
    assert [num for _, num, _ in _geometry_headings(blocks)] == ["1", "2", "3.1"]


def test_cross_page_grid_is_stitched():
    a = _grid(5, 600, 740, ["Lvl", "Conc", "Area"], [["4", "0.78", "5118"], ["5", "0.97", "6645"]])
    b = _grid(6, 80, 740, ["6", "1.17", "7594"], [["7", "1.46", "9720"]])
    out = _stitch_cross_page_tables([a, b], {5: 792.0, 6: 792.0})
    assert len(out) == 1
    assert out[0]["continues_to"] is True
    assert out[0]["source_pages"] == [5, 6]
    assert ["6", "1.17", "7594"] in out[0]["rows"]  # B's first row was data, kept
    assert ["7", "1.46", "9720"] in out[0]["rows"]


def test_no_overstitch_when_middle_fragment_does_not_reach_bottom():
    a = _grid(5, 600, 740, ["H1", "H2", "H3"], [["r", "s", "t"]])
    b = _grid(6, 80, 300, ["6", "x", "y"], [["r", "s", "t"]])  # ends mid-page
    c = _grid(7, 80, 160, ["7", "x", "y"], [["r", "s", "t"]])
    out = _stitch_cross_page_tables([a, b, c], {5: 792.0, 6: 792.0, 7: 792.0})
    assert len(out) == 2
    assert out[0]["source_pages"] == [5, 6]  # a+b stitched, c left separate


def test_unrelated_tables_not_stitched():
    a = _grid(5, 600, 740, ["A", "B"], [["1", "2"]])
    b = _grid(6, 80, 160, ["X", "Y", "Z"], [["1", "2", "3"]])
    assert len(_stitch_cross_page_tables([a, b], {5: 792.0, 6: 792.0})) == 2  # different n_cols


def test_tables_only_document_surfaces_them():
    table = _grid(2, 0, 10, ["A", "B"], [["1", "2"]])
    doc = {
        "filename": "x.pdf",
        "page_count": 2,
        "toc": [],
        "pages": [
            {"page_number": 1, "height": 792.0, "blocks": [], "tables": [], "figures": []},
            {"page_number": 2, "height": 792.0, "blocks": [], "tables": [table], "figures": []},
        ],
    }
    sections = split_document(doc)
    assert len(sections) == 1
    assert sections[0]["tables"][0]["headers"] == ["A", "B"]


def test_render_section_produces_markdown_with_values():
    """Detection agents receive the section as markdown with tables + provenance."""
    from regwatch.deficiency.detection.render import render_section

    section = {
        "heading": "Appendix 2",
        "page_start": 29,
        "page_end": 30,
        "blocks": [{"role": "paragraph", "text": "Certificate of Analysis"}],
        "tables": [
            {
                "kind": "key_value",
                "title": "CoA",
                "page": 29,
                "pairs": [{"label": "Purity", "value": "98.4%"}],
            }
        ],
        "figures": [],
    }
    md = render_section(section)
    assert "Appendix 2" in md
    assert "Certificate of Analysis" in md
    assert "Purity" in md and "98.4%" in md
