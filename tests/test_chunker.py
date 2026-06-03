"""Chunker tests: page metadata is preserved on every chunk."""

from __future__ import annotations

from regwatch.process.chunker import chunk_pdf


def test_each_chunk_has_page_and_metadata() -> None:
    pages = [
        "I. Introduction\nThis guidance describes bioequivalence study recommendations.",
        "II. Recommendations\nA. Type of Study: Two studies are recommended.",
        "B. Dissolution: USP Apparatus 2 at 50 RPM.",
    ]
    base = {
        "doc_id": 1,
        "version_id": 1,
        "normalized_name": "albuterol sulfate",
        "source_url": "http://example/PSG_020503.pdf",
    }
    chunks = chunk_pdf(pages, base_metadata=base)
    assert chunks, "chunker produced no chunks"
    for c in chunks:
        assert 1 <= c.page <= len(pages)
        assert c.metadata["doc_id"] == 1
        assert c.metadata["normalized_name"] == "albuterol sulfate"
        assert "source_url" in c.metadata


def test_no_chunk_loses_source_or_page() -> None:
    """Spec §10.3 acceptance: no chunk loses its source/page metadata."""
    pages = ["one\ntwo\nthree", "alpha\nbeta\ngamma"]
    base = {"doc_id": 9, "version_id": 1, "source_url": "u"}
    chunks = chunk_pdf(pages, base_metadata=base)
    for c in chunks:
        assert "source_url" in c.metadata and c.metadata["source_url"] == "u"
        assert isinstance(c.page, int) and c.page >= 1


def test_sliding_window_emits_multiple_chunks_for_long_section() -> None:
    big = "alpha " * 2500  # ~12.5k chars, > 1000-token target
    chunks = chunk_pdf([big], base_metadata={"doc_id": 1, "version_id": 1, "source_url": "u"})
    assert len(chunks) > 1
    # Overlap → adjacent chunks share at least some text.
    a, b = chunks[0].text, chunks[1].text
    assert a[-100:] in b[:300] or any(tok in b[:200] for tok in a[-100:].split())
