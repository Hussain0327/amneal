"""Rebuilding a PSG's stored chunks into studio blocks.

The parser has one job that matters: reproduce FDA's words, in FDA's order,
without inventing structure. These cases pin the decisions that were made
against real corpus text -- where a paragraph ends, what a heading is, and
what must never be glued to what.
"""

from __future__ import annotations

from regwatch.process.psg_document import (
    PsgChunkText,
    build_body,
    document_file_name,
)

# A page's text as pdfplumber yields it: hard-wrapped near a fixed width, no
# blank lines between paragraphs. Width matters -- the parser measures it.
_PAGE_1 = """Draft Guidance on Semaglutide
December 2025
Active Ingredient: Semaglutide
Dosage Form: Solution
Route: Subcutaneous
Recommended Study: Request for waiver of in vivo bioequivalence study
To qualify for a waiver from submitting an in vivo bioequivalence study on the
basis that bioequivalence is self-evident under 21 CFR 320.22(b)(1), a generic
semaglutide subcutaneous solution product should be qualitatively the same as
the reference listed drug (RLD).
1 Q1 (Qualitative sameness) means that the test product uses the same inactive
ingredient(s) as the RLD."""


def _chunks(*pages: str) -> list[PsgChunkText]:
    """One chunk per page, in page order."""
    return [PsgChunkText(ordinal=i, page=i + 1, text=text) for i, text in enumerate(pages)]


def _texts(pages: list[str]) -> list[tuple[str, str]]:
    """(type, text) for every rebuilt block, for compact assertions."""
    body = build_body(7, _chunks(*pages))
    return [(b.type, b.text) for b in body.blocks]


def test_title_and_date_are_separate_blocks() -> None:
    blocks = _texts([_PAGE_1])
    assert blocks[0] == ("title", "Draft Guidance on Semaglutide")
    assert blocks[1] == ("meta", "December 2025")


def test_title_never_absorbs_the_paragraph_below_it() -> None:
    # Older PSGs open with a disclaimer paragraph. A title carries no closing
    # punctuation, so only the "a title is one line" rule keeps them apart.
    blocks = _texts(
        [
            "Guidance on Chloroquine Phosphate\n"
            "This guidance represents the current thinking of the Food and Drug\n"
            "Administration on this topic."
        ]
    )
    assert blocks[0] == ("title", "Guidance on Chloroquine Phosphate")
    assert blocks[1][0] == "p"
    assert blocks[1][1].startswith("This guidance represents")


def test_the_date_line_never_absorbs_the_paragraph_below_it() -> None:
    # An older PSG opens title / date / disclaimer. A date line carries no
    # terminal punctuation and no colon, so nothing else would break it, and
    # the disclaimer would print in the small metadata face glued to the date.
    blocks = _texts(
        [
            "Draft Guidance on Chloroquine Phosphate\n"
            "December 2025\n"
            "This guidance represents the current thinking of the Food and Drug\n"
            "Administration on this topic."
        ]
    )
    assert blocks[0] == ("title", "Draft Guidance on Chloroquine Phosphate")
    assert blocks[1] == ("meta", "December 2025")
    assert blocks[2][0] == "p"
    assert blocks[2][1].startswith("This guidance represents")


def test_a_footnote_still_joins_its_own_wrapped_lines() -> None:
    # The date-line rule must not spill over onto footnotes: their
    # continuation lines belong to them.
    blocks = _texts(
        [
            "Title line\n"
            "1 Q1 (Qualitative sameness) means that the test product uses the same\n"
            "inactive ingredient(s) as the RLD."
        ]
    )
    footnotes = [t for kind, t in blocks if kind == "meta"]
    assert footnotes == [
        "1 Q1 (Qualitative sameness) means that the test product uses the same "
        "inactive ingredient(s) as the RLD."
    ]


def test_label_lines_stay_whole_and_separate() -> None:
    blocks = _texts([_PAGE_1])
    assert ("p", "Active Ingredient: Semaglutide") in blocks
    assert ("p", "Dosage Form: Solution") in blocks
    assert ("p", "Route: Subcutaneous") in blocks


def test_wrapped_prose_rejoins_into_one_paragraph() -> None:
    body = [text for kind, text in _texts([_PAGE_1]) if kind == "p"]
    joined = next(t for t in body if t.startswith("To qualify"))
    assert "\n" not in joined
    assert "bioequivalence study on the basis that" in joined
    assert joined.endswith("the reference listed drug (RLD).")


def test_footnote_is_not_glued_to_the_body_above_it() -> None:
    blocks = _texts([_PAGE_1])
    footnote = [t for kind, t in blocks if kind == "meta" and t.startswith("1 Q1")]
    assert len(footnote) == 1
    assert "reference listed drug" not in footnote[0]


def test_bare_label_is_a_heading_and_a_valued_label_is_not() -> None:
    blocks = _texts(["Title line\nAdditional information:\nDevice: prefilled autoinjector."])
    assert ("h2", "Additional information:") in blocks
    assert ("p", "Device: prefilled autoinjector.") in blocks


def test_a_heading_never_absorbs_the_prose_under_it() -> None:
    # Holds even on a document too narrow for the paragraph-width rule to
    # apply: a heading is one line, the same way a title is.
    blocks = _texts(["Title\nRecommended Studies:\nThree in vitro studies are recommended."])
    assert ("h2", "Recommended Studies:") in blocks
    assert ("p", "Three in vitro studies are recommended.") in blocks


def test_roman_and_option_headings() -> None:
    blocks = _texts(["Title line\nI. Study design\nOption 2: In vitro studies"])
    assert ("h2", "I. Study design") in blocks
    assert ("h2", "Option 2: In vitro studies") in blocks


def test_hyphenated_line_break_rejoins_without_a_space() -> None:
    blocks = _texts(
        [
            "Title line\n"
            "Levels of non-\n"
            "peptide process-related impurities should meet compendial limits."
        ]
    )
    assert any("non-peptide process-related" in text for _, text in blocks)


def test_a_line_opening_with_digits_continues_the_sentence() -> None:
    # "...pursuant to 21 CFR" / "320.22(c) provided that..." is one sentence;
    # only a capital letter may open a new paragraph.
    blocks = _texts(
        [
            "Title line\n"
            "Waiver request of in vivo testing: EQ 150 mg Base, pursuant to 21 CFR\n"
            "320.22(c) provided that dissolution profiles are comparable."
        ]
    )
    assert any("21 CFR 320.22(c) provided that" in text for _, text in blocks)


def test_sentence_carries_over_a_page_break() -> None:
    blocks = _texts(
        [
            "Title line\nThe applicant should demonstrate that the proposed generic and",
            "the reference standard are comparable.",
        ]
    )
    assert any(
        "proposed generic and the reference standard are comparable." in text for _, text in blocks
    )


def test_finished_sentence_does_not_carry_over_a_page_break() -> None:
    blocks = _texts(
        [
            "Title line\nThe applicant should demonstrate comparability.",
            "A second paragraph begins on the next page.",
        ]
    )
    assert ("p", "The applicant should demonstrate comparability.") in blocks
    assert ("p", "A second paragraph begins on the next page.") in blocks


def test_overlapping_chunks_on_one_page_are_not_duplicated() -> None:
    # The chunker overlaps windows by ~150 tokens; the repeat is verbatim.
    tail = "the same inactive ingredients as the reference listed drug."
    body = build_body(
        7,
        [
            PsgChunkText(ordinal=0, page=1, text=f"Title line\nA product should use {tail}"),
            PsgChunkText(ordinal=1, page=1, text=f"{tail} Further recommendations follow."),
        ],
    )
    rendered = " ".join(b.text for b in body.blocks)
    assert rendered.count("the same inactive ingredients") == 1
    assert "Further recommendations follow." in rendered


def test_a_one_character_coincidence_is_not_mistaken_for_overlap() -> None:
    # The first chunk ends with "T" and the second begins with "T". Treating
    # that as an overlap would delete the T from "The" -- silently, in a
    # document an analyst is about to cite.
    body = build_body(
        7,
        [
            PsgChunkText(ordinal=0, page=1, text="Title line\nStudies should measure T"),
            PsgChunkText(ordinal=1, page=1, text="The applicant should justify the method."),
        ],
    )
    rendered = " ".join(b.text for b in body.blocks)
    assert "The applicant should justify" in rendered


def test_page_numbers_are_carried_on_every_block() -> None:
    body = build_body(7, _chunks("Title line\nFirst page prose.", "Second page prose."))
    assert [b.page for b in body.blocks] == [1, 1, 2]


def test_block_ids_are_unique_and_namespaced_by_document() -> None:
    body = build_body(7, _chunks(_PAGE_1))
    ids = [b.id for b in body.blocks]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("psg-7-b") for i in ids)


def test_empty_input_rebuilds_to_nothing_rather_than_an_empty_document() -> None:
    body = build_body(7, [])
    assert body.blocks == []
    assert body.truncated is False


def test_blank_and_whitespace_only_chunks_are_dropped() -> None:
    body = build_body(7, [PsgChunkText(ordinal=0, page=1, text="   \n\n  ")])
    assert body.blocks == []


def test_runaway_document_is_truncated_and_says_so() -> None:
    # Each line is its own block (every one is a label), so 900 lines exceeds
    # the 600-block guard.
    page = "\n".join(f"Section {i}: value" for i in range(900))
    body = build_body(7, _chunks(page))
    assert body.truncated is True
    assert len(body.blocks) == 600


def test_file_name_uses_fdas_identifier_when_there_is_one() -> None:
    assert (
        document_file_name(appl_no="020503", active_ingredient="Albuterol Sulfate")
        == "PSG_020503 Albuterol Sulfate.docx"
    )


def test_file_name_without_an_application_number() -> None:
    assert (
        document_file_name(appl_no=None, active_ingredient="Albuterol Sulfate")
        == "Albuterol Sulfate PSG.docx"
    )


def test_file_name_strips_punctuation_from_combination_products() -> None:
    assert (
        document_file_name(
            appl_no="bad-value", active_ingredient="Ethinyl Estradiol; Levonorgestrel"
        )
        == "Ethinyl Estradiol Levonorgestrel PSG.docx"
    )
