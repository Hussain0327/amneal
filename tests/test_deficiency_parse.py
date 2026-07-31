"""Ported from upstream tests/unit/test_parse.py (DefPredict).

Deviation beyond the import-rewrite map: upstream hardcoded two specific vendored sample
PDFs (32s41-Specification.pdf: exactly 13 pages / 32s43-validation-related-compounds-
method.pdf: exactly 55 pages, >=10 tables, etc.) from a local Desktop path. Neither file
is committed here, and this repo's fixture seam is the single
REGWATCH_DEFICIENCY_SAMPLE_PDF env var (one PDF, not two), so the exact per-file counts
cannot be reproduced. The exact-count assertions are relaxed to structural invariants that
hold for any real CMC submission PDF that extract_pdf/split_document can process; the
document-shape assertions (key sets, "heading" present, group size cap) are unchanged.
The whole module is skipped when the env var is unset.
"""

import os

import pytest

from regwatch.deficiency.parse.pdf import extract_pdf
from regwatch.deficiency.parse.section_splitter import group_sections, split_document

SAMPLE_PDF = os.environ.get("REGWATCH_DEFICIENCY_SAMPLE_PDF", "")

skip_if_no_sample = pytest.mark.skipif(
    not SAMPLE_PDF or not os.path.exists(SAMPLE_PDF),
    reason="REGWATCH_DEFICIENCY_SAMPLE_PDF not set",
)


@skip_if_no_sample
class TestSamplePDF:
    def test_extract_pages(self):
        doc = extract_pdf(SAMPLE_PDF)
        assert doc["page_count"] > 0
        assert len(doc["pages"]) == doc["page_count"]
        assert doc["filename"] == os.path.basename(SAMPLE_PDF)

    def test_extract_returns_json(self):
        doc = extract_pdf(SAMPLE_PDF)
        assert set(doc.keys()) == {"filename", "page_count", "toc", "pages"}
        page = doc["pages"][0]
        assert {"blocks", "tables", "figures", "source", "is_scanned"} <= set(page.keys())

    def test_split_produces_section_dicts(self):
        sections = split_document(extract_pdf(SAMPLE_PDF))
        assert len(sections) >= 1
        assert all(isinstance(s, dict) and "heading" in s for s in sections)

    def test_group_sections(self):
        sections = split_document(extract_pdf(SAMPLE_PDF))
        groups = group_sections(sections, max_sections_per_group=3)
        assert len(groups) >= 1
        assert all(len(g["sections"]) <= 3 for g in groups)

    def test_sections_have_text(self):
        sections = split_document(extract_pdf(SAMPLE_PDF))
        for s in sections:
            assert len(s["text"]) > 0
