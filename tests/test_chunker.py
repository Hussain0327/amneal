"""Chunker tests: page metadata is preserved on every chunk, and the v2
recipe fixes the failure classes the 2026-07-30 corpus audit measured
(stranded headings, list-item section identity, heading loss across pages,
page furniture, mid-word window splits)."""

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
        assert c.metadata["ordinal"] == c.ordinal


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


def test_numbered_list_items_do_not_split_sections() -> None:
    """Arabic-numbered lines are list items, not section boundaries. v1 split
    on them, which promoted list sentences to section_path identity and
    stranded the real heading as a tiny chunk (audit class A)."""
    lines = ["II. Option 2: One in vivo bioequivalence study with pharmacokinetic endpoints"]
    lines += [f"{i}. Requirement line {i} " + "detail " * 20 for i in range(1, 6)]
    chunks = chunk_pdf(
        ["\n".join(lines)], base_metadata={"doc_id": 1, "version_id": 1, "source_url": "u"}
    )
    assert len(chunks) == 1
    c = chunks[0]
    assert c.section_path is not None and c.section_path.startswith("II Option 2:")
    assert c.text.startswith("II. Option 2:")
    assert "3. Requirement line 3" in c.text


def test_heading_carries_across_pages() -> None:
    """A section wrapping across a page break keeps its parent heading; v1
    reset section_path to None on every page (audit KG-finding 3)."""
    page1 = "I. Introduction\n" + "This guidance describes the requirements. " * 12
    page2 = "Continuation of the introduction body without any header. " * 8
    chunks = chunk_pdf(
        [page1, page2], base_metadata={"doc_id": 1, "version_id": 1, "source_url": "u"}
    )
    page2_chunks = [c for c in chunks if c.page == 2]
    assert page2_chunks
    assert all(c.section_path == "I Introduction" for c in page2_chunks)


def test_small_section_merges_forward_with_heading() -> None:
    """A heading with a tiny body is never emitted alone: it travels as the
    first line of the chunk it introduces (audit class A fix)."""
    page = "A. Dissolution\nB. Study design\n" + "The study uses USP apparatus 2 at 50 rpm. " * 10
    chunks = chunk_pdf([page], base_metadata={"doc_id": 1, "version_id": 1, "source_url": "u"})
    assert len(chunks) == 1
    assert chunks[0].text.startswith("A. Dissolution")
    assert "B. Study design" in chunks[0].text


def test_page_furniture_and_disclaimer_stripped() -> None:
    """Fixed furniture lines, per-document repeated lines (running title), and
    the FDA disclaimer paragraph never reach chunk text; body content and the
    product-identification block survive (audit classes E and F)."""
    title = "Guidance on Albuterol Sulfate"
    footer = "Recommended Sep 2012; Revised Mar 2015, Aug 2024"
    disclaimer = (
        "This draft guidance, when finalized, will represent the current thinking of "
        "the Food and Drug Administration on this topic. It does not create any rights "
        "for any person and is not binding."
    )

    def page(n: int, body: str) -> str:
        return "\n".join([title, "Contains Nonbinding Recommendations", body, f"{footer} {n}"])

    body1 = "Active Ingredient: Albuterol sulfate\n" + "The study enrolls healthy subjects. " * 10
    pages = [
        page(1, disclaimer + "\n" + body1),
        page(2, "Second page body content. " * 15),
        page(3, "Third page body content. " * 15),
    ]
    chunks = chunk_pdf(pages, base_metadata={"doc_id": 1, "version_id": 1, "source_url": "u"})
    all_text = "\n".join(c.text for c in chunks)
    assert "Contains Nonbinding Recommendations" not in all_text
    assert "Revised Mar 2015" not in all_text
    assert title not in all_text
    assert "when finalized" not in all_text
    assert "Active Ingredient: Albuterol sulfate" in all_text
    assert "Second page body content." in all_text


def test_sliding_window_never_splits_mid_word() -> None:
    """The v1 raw character slice could cut a word (and once did, corpus-wide
    chunk 219-1801-16 opening 'uivalence...'); v2 backs up to a boundary."""
    words = [f"word{i:05d}" for i in range(2000)]
    chunks = chunk_pdf(
        [" ".join(words)], base_metadata={"doc_id": 1, "version_id": 1, "source_url": "u"}
    )
    assert len(chunks) > 3
    vocab = set(words)
    for c in chunks:
        toks = c.text.split()
        assert toks[0] in vocab, f"chunk starts mid-word: {toks[0]!r}"
        assert toks[-1] in vocab, f"chunk ends mid-word: {toks[-1]!r}"


def test_genus_species_line_is_not_promoted_to_a_section_header() -> None:
    """`E. coli` at the start of a line must stay prose, not become a section.

    `[A-Z]\\.` matched the genus abbreviation opening a species name, so a line
    beginning "E. coli is the primary pathogen of interest." parsed as marker
    "E." plus a heading made of the rest of the sentence -- prose promoted into
    section_path, the field INV-1 citations resolve against. Anti-infective
    labelling routinely wraps so species names open lines, hence the multi-line
    fixture below.
    """
    # BOTH paragraphs must clear MIN_SECTION_CHARS on their own. A short real
    # section would forward-merge into the false species section and claim it
    # via the carry-attribution rule, and a short false section would merge away
    # entirely -- either way the bogus path disappears and the regex defect this
    # test pins goes undetected.
    intro_paragraph = (
        "The indication covers complicated urinary tract infections in adult\n"
        "patients with limited or no alternative treatment options, including\n"
        "pyelonephritis, and the clinical program enrolled subjects across\n"
        "forty sites in twelve countries with stratification by baseline renal\n"
        "function and prior antibacterial exposure within thirty days.\n"
    )
    species_paragraph = (
        "E. coli is the primary pathogen of interest.\n"
        "Susceptibility testing should follow current CLSI breakpoints for each\n"
        "organism listed in the approved labelling, and isolates should be\n"
        "characterised by pulsed-field gel electrophoresis where available.\n"
        "Resistance rates observed in the pivotal studies were below five\n"
        "percent for all organisms tested across both treatment arms.\n"
    )
    pages = ["II. Microbiology\n" + intro_paragraph + species_paragraph]
    base = {"doc_id": 1, "version_id": 1, "normalized_name": "x", "source_url": "u"}
    chunks = chunk_pdf(pages, base_metadata=base)

    paths = [c.section_path for c in chunks if c.section_path]
    assert paths, "expected the real II. Microbiology heading to survive"
    for path in paths:
        assert "coli" not in path, path
        assert "aureus" not in path, path
        assert "pylori" not in path, path
    assert any(path.startswith("II Microbiology") for path in paths)


def test_real_lettered_headings_still_parse_after_the_species_guard() -> None:
    """The guard must not cost us genuine headers, colon subtitles included."""
    filler = "Additional protocol detail follows in this paragraph. " * 5
    pages = [f"II. Recommendations\nA. Type of Study: Two studies\n{filler}"]
    base = {"doc_id": 1, "version_id": 1, "normalized_name": "x", "source_url": "u"}
    chunks = chunk_pdf(pages, base_metadata=base)

    paths = [c.section_path for c in chunks if c.section_path]
    assert any("Type of Study" in path for path in paths), paths


def test_forward_merged_chunk_keeps_the_section_its_text_starts_in() -> None:
    """A chunk whose text opens in section A must not be cited as section B.

    The merge unconditionally preferred the section being merged INTO, so a
    chunk beginning "A. Dissolution ..." was labelled with B's path. An answer
    citing that chunk would name a section the quoted sentence is not in.
    """
    a_prose = "USP Apparatus 2 at 50 RPM in 900 mL of dissolution medium."
    b_filler = "Conduct a fasting single-dose bioequivalence study. " * 6
    pages = [f"A. Dissolution\n{a_prose}\nB. Study design\n{b_filler}"]
    base = {"doc_id": 1, "version_id": 1, "normalized_name": "x", "source_url": "u"}
    chunks = chunk_pdf(pages, base_metadata=base)

    opening = next(c for c in chunks if "Apparatus 2" in c.text)
    assert opening.section_path is not None
    assert opening.section_path.startswith("A Dissolution"), opening.section_path


def test_a_stranded_heading_does_not_claim_the_section_it_introduces() -> None:
    """A heading with no prose beneath it has nothing to cite, so it must not win.

    This is the case the forward merge exists for, and the attribution fix must
    leave it intact: the bare heading travels as the first line of the chunk it
    introduces while that chunk keeps its own identity.
    """
    b_filler = "Conduct a fasting single-dose bioequivalence study. " * 6
    pages = [f"A. Dissolution\nB. Study design\n{b_filler}"]
    base = {"doc_id": 1, "version_id": 1, "normalized_name": "x", "source_url": "u"}
    chunks = chunk_pdf(pages, base_metadata=base)

    merged = next(c for c in chunks if "Study design" in c.text)
    assert merged.section_path is not None
    assert merged.section_path.startswith("B Study design"), merged.section_path
