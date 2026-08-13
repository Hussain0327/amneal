"""The Studio-blocks -> structured-document adapter (step 1 of the Studio check).

These tests exist to answer one question: can the vendored DefPredict pipeline
consume a document that never came from a PDF? The adapter fabricates the
geometry a parser would have measured, so every test here pins a place where a
plausible fabrication would quietly change what the pipeline sees rather than
raise -- a check that returns zero faults because a filter dropped the only
page is far worse than one that crashes.

Several tests document a LIMITATION rather than a guarantee. Those are the
honest half: they make the coverage this seam does not have visible to whoever
builds the endpoint on top of it, instead of leaving it to be discovered.

Fixtures are hand-written dicts, matching tests/test_deficiency_section_splitter.py.
"""

from typing import Any

import pytest

from regwatch.deficiency.blocks import doc_from_blocks
from regwatch.deficiency.detection.checklists import _reading_order_text
from regwatch.deficiency.detection.oracles import result_vs_limit, run_oracles
from regwatch.deficiency.detection.verify import verify_and_tier
from regwatch.deficiency.parse.section_splitter import split_document
from regwatch.deficiency.schemas.faults import EvidenceClass, Fault

# The keys extract_pdf emits (parse/pdf.py) and the LayoutBlock fields it dumps.
# Parity with these is the whole contract: section_splitter reads several of
# them unguarded, so a missing key is a crash and a spurious one is a lie.
DOC_KEYS = {"filename", "page_count", "toc", "pages"}
PAGE_KEYS = {
    "page_number",
    "page_label",
    "width",
    "height",
    "rotation",
    "source",
    "is_scanned",
    "blocks",
    "tables",
    "figures",
}
BLOCK_KEYS = {"role", "text", "bbox", "page", "reading_order", "confidence", "style", "lines"}


def _p(block_id: str, text: str) -> dict[str, Any]:
    return {"id": block_id, "type": "p", "text": text}


def _h2(block_id: str, text: str) -> dict[str, Any]:
    return {"id": block_id, "type": "h2", "text": text}


def _table(block_id: str, text: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": block_id, "type": "table", "text": text, "rows": rows}


def _row(cells: list[str], head: bool = False) -> dict[str, Any]:
    return {"cells": cells, "head": head}


def test_document_shape_matches_extract_pdf_keys():
    doc = doc_from_blocks("Spec.docx", [_p("b1", "Assay 98.2 percent.")])

    assert set(doc) == DOC_KEYS
    assert len(doc["pages"]) == 1
    assert set(doc["pages"][0]) == PAGE_KEYS
    assert set(doc["pages"][0]["blocks"][0]) == BLOCK_KEYS


def test_page_is_taller_than_every_block_on_it():
    # section_splitter reads height unguarded and treats 0.0 as falsy, which
    # disarms both cross-page stitch guards instead of raising.
    doc = doc_from_blocks("Spec.docx", [_p("b1", "one"), _p("b2", "two")])
    page = doc["pages"][0]

    assert page["width"] > 0
    assert page["height"] > max(b["bbox"][3] for b in page["blocks"])


def test_the_only_page_survives_the_cover_page_drop():
    # _kept_pages drops page 1 as a cover sheet on any document of more than
    # two pages. A synthetic document must therefore stay at one page, or its
    # entire content disappears before detection ever runs.
    doc = doc_from_blocks("Spec.docx", [_p("b1", "Dissolution is not less than 80 percent.")])

    sections = split_document(doc)

    assert doc["page_count"] == 1
    assert sections
    assert "Dissolution is not less than 80 percent." in sections[0]["text"]


def test_a_paginated_document_would_lose_its_only_page():
    # Why the value above must be 1, demonstrated rather than asserted: the
    # same document claiming three pages returns no sections and no error.
    doc = doc_from_blocks("Spec.docx", [_p("b1", "Dissolution is not less than 80 percent.")])
    doc["page_count"] = 3

    assert split_document(doc) == []


def test_block_text_is_preserved_verbatim():
    # A fault quotes the document and the Studio anchors that quote back into
    # the block it came from. Any normalisation here breaks that anchor.
    text = "Assay:  95.0 - 105.0%  (per USP)"
    doc = doc_from_blocks("Spec.docx", [_p("b1", text)])

    assert doc["pages"][0]["blocks"][0]["text"] == text


def test_quoted_evidence_anchors_against_the_document():
    # The reason verbatim matters, end to end: verify_and_tier grades a fault
    # by whether its evidence is findable in the document it came from.
    doc = doc_from_blocks("Spec.docx", [_p("b1", "The shelf life proposed is 24 months.")])

    graded = {
        f.title: f.evidence_class
        for f in verify_and_tier(
            [
                Fault(title="Quoted", evidence="shelf life proposed is 24 months"),
                Fault(title="Invented", evidence="the sponsor committed to a 36 month study"),
            ],
            doc,
        )
    }

    assert graded["Quoted"] == EvidenceClass.QUOTE_ANCHORED
    assert graded["Invented"] == EvidenceClass.MODEL_JUDGMENT


def test_blocks_keep_input_order_through_the_splitter():
    doc = doc_from_blocks(
        "Spec.docx",
        [_p("b1", "First paragraph."), _p("b2", "Second paragraph."), _p("b3", "Third paragraph.")],
    )

    section_text = split_document(doc)[0]["text"]

    assert section_text.index("First") < section_text.index("Second") < section_text.index("Third")


def test_reading_order_is_dense():
    # split_document sorts on (page, reading_order). A constant reading_order
    # would leave the order to the sort's stability rather than to the document.
    doc = doc_from_blocks("Spec.docx", [_p("b1", "one"), _p("b2", "two"), _p("b3", "three")])

    orders = [b["reading_order"] for b in doc["pages"][0]["blocks"]]

    assert orders == [0, 1, 2]


def test_block_top_coordinates_increase_down_the_page():
    # _position orders tables and figures against block tops. Identical tops
    # would collapse that ordering to whichever item was appended first.
    doc = doc_from_blocks("Spec.docx", [_p("b1", "one"), _p("b2", "two"), _p("b3", "three")])

    tops = [b["bbox"][1] for b in doc["pages"][0]["blocks"]]

    assert tops == sorted(tops)
    assert len(set(tops)) == len(tops)


def test_every_studio_type_becomes_body_text():
    # page_header/page_footer is the one role pair the splitter, the oracles,
    # the checklists and the prompt renderer all drop. Mapping a title or a
    # meta line onto either would delete the product identity from all four.
    doc = doc_from_blocks(
        "Spec.docx",
        [
            {"id": "t", "type": "title", "text": "Metformin ER 500 mg Specification"},
            {"id": "m", "type": "meta", "text": "Effective 2026-01-01"},
            _h2("h", "Description"),
            _p("p", "The product is an extended release tablet."),
            _table("tb", "Assay 98.2 percent", []),
        ],
    )

    roles = {b["role"] for b in doc["pages"][0]["blocks"]}

    assert roles == {"paragraph"}
    assert "metformin er 500 mg specification" in _reading_order_text(doc)


def test_table_block_becomes_a_grid_table():
    # The deterministic oracles read page tables, never block text. A table
    # flattened to prose is invisible to every code-verified check we own.
    doc = doc_from_blocks(
        "Spec.docx",
        [
            _table(
                "t1",
                "Test Acceptance Criterion Result",
                [
                    _row(["Test", "Acceptance Criterion", "Result"], head=True),
                    _row(["Assay", "NLT 95.0%", "98.2%"]),
                ],
            )
        ],
    )

    tables = doc["pages"][0]["tables"]

    assert len(tables) == 1
    assert tables[0]["kind"] == "grid"
    assert tables[0]["headers"] == ["Test", "Acceptance Criterion", "Result"]
    assert tables[0]["rows"] == [["Assay", "NLT 95.0%", "98.2%"]]
    assert tables[0]["n_cols"] == 3


def test_result_vs_limit_oracle_fires_on_an_adapted_table():
    # The end-to-end proof that the seam holds: a deterministic oracle produces
    # a fault from a document the Studio built, with no PDF anywhere.
    doc = doc_from_blocks(
        "Spec.docx",
        [
            _table(
                "t1",
                "",
                [
                    _row(["Test", "Acceptance Criterion", "Result"], head=True),
                    _row(["Assay", "NLT 95.0%", "92.4%"]),
                ],
            )
        ],
    )

    faults = result_vs_limit(doc)

    assert len(faults) == 1
    assert "Assay" in faults[0].title


def test_a_table_without_cells_falls_back_to_text_and_leaves_the_oracles_dark():
    # A LIMITATION, pinned. The Studio can hold a table it has no cell model
    # for. Its content is kept as prose rather than dropped, but the two
    # table-bound oracles cannot see an out-of-specification number in prose.
    doc = doc_from_blocks("Spec.docx", [_table("t1", "Assay NLT 95.0% result 92.4%", [])])

    page = doc["pages"][0]

    assert page["tables"] == []
    assert [b["text"] for b in page["blocks"]] == ["Assay NLT 95.0% result 92.4%"]
    assert run_oracles(doc) == []


def test_a_document_with_no_text_is_refused():
    # An empty document still runs the whole pipeline and reports a clean
    # result. A compliance check must not fabricate that outcome.
    with pytest.raises(ValueError):
        doc_from_blocks("Empty.docx", [])

    with pytest.raises(ValueError):
        doc_from_blocks("Empty.docx", [_p("b1", "   "), _p("b2", "\n")])


def test_a_block_without_text_is_a_wire_error_not_an_empty_document():
    with pytest.raises(KeyError):
        doc_from_blocks("Spec.docx", [{"id": "b1", "type": "p"}])


def test_whitespace_only_blocks_are_dropped():
    # _build_section joins on truthiness, so "   " would contribute a blank
    # line to the section text and a wasted slot to heading detection.
    doc = doc_from_blocks("Spec.docx", [_p("b1", "   "), _p("b2", "Real content.")])

    assert [b["text"] for b in doc["pages"][0]["blocks"]] == ["Real content."]


def test_numbered_headings_split_the_document_into_sections():
    doc = doc_from_blocks(
        "Spec.docx",
        [
            _h2("h1", "1. Description"),
            _p("b1", "The drug product is an extended release tablet."),
            _h2("h2", "2. Acceptance Criteria"),
            _p("b2", "Assay is not less than 95.0 percent of label claim."),
        ],
    )

    headings = [s["heading"] for s in split_document(doc)]

    assert headings == ["1 Description", "2 Acceptance Criteria"]


def test_unnumbered_headings_yield_a_single_section():
    # A LIMITATION, pinned: the splitter identifies headings by leading section
    # NUMBER, not by role, and the adapter does not fabricate a table of
    # contents to fake one. No text is lost, only section granularity.
    doc = doc_from_blocks(
        "Spec.docx",
        [
            _h2("h1", "Description"),
            _p("b1", "The drug product is an extended release tablet."),
            _h2("h2", "Acceptance Criteria"),
            _p("b2", "Assay is not less than 95.0 percent of label claim."),
        ],
    )

    sections = split_document(doc)

    assert len(sections) == 1
    assert sections[0]["heading"] == "Spec.docx"


def test_a_short_preamble_is_absent_from_sections_but_still_in_the_document():
    # A LIMITATION, pinned, and the sharpest one. The splitter only promotes
    # text before the first heading into a section once it exceeds 200
    # characters, so a title and a meta line ahead of a numbered heading reach
    # no section at all. They DO still reach the oracles, the checklists and
    # the anchoring corpus, which all read the page directly -- so this is lost
    # section granularity, not lost content. The threshold is shared with the
    # PDF path, so it is not the adapter's to change.
    title = "Metformin ER 500 mg Specification"
    doc = doc_from_blocks(
        "Spec.docx",
        [
            {"id": "t", "type": "title", "text": title},
            {"id": "m", "type": "meta", "text": "Effective 2026-01-01"},
            _h2("h1", "1. Description"),
            _p("b1", "The drug product is an extended release tablet."),
        ],
    )

    sections = split_document(doc)

    assert [s["heading"] for s in sections] == ["1 Description"]
    assert all(title not in s["text"] for s in sections)
    assert any(b["text"] == title for b in doc["pages"][0]["blocks"])
