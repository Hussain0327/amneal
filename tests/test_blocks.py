"""Block segmentation: markdown structure becomes claim units, not glue.

The prose parser used to split a completion on sentence punctuation alone, so
a ``## Heading`` line (no terminator) was welded onto the sentence after it
and every table collapsed into one unterminated string. ``segment`` reads the
markdown shape FIRST -- paragraphs, headings, list items, table cells -- and
only then splits sentences inside each block, so the gate still sees one
sentence per claim and the renderer can rebuild the structure it came from.
"""

from __future__ import annotations

import pytest

from regwatch.common import blocks
from regwatch.common.blocks import Block, segment, split_units

pytestmark = pytest.mark.invariants


def _shape(text: str) -> list[tuple[str, str, int, int, int]]:
    return [
        (u.text, u.block.kind, u.block.group, u.block.item, u.block.cell) for u in segment(text)
    ]


def test_heading_is_its_own_unit_not_glued_to_the_next_sentence() -> None:
    """The production symptom: "Recommended BE design ... FDA's draft PSG
    recommends" ran together because the heading had no terminator."""
    text = "## Recommended BE design\n\nFDA recommends a fasting study [1]."
    assert _shape(text) == [
        ("Recommended BE design", blocks.BLOCK_HEADING, 0, 0, 0),
        ("FDA recommends a fasting study [1].", blocks.BLOCK_PARAGRAPH, 1, 0, 0),
    ]
    assert segment(text)[0].block.level == 2


def test_heading_without_a_blank_line_after_it_still_separates() -> None:
    text = "### Design\nA fasting study is recommended [1]."
    assert [u.block.kind for u in segment(text)] == [
        blocks.BLOCK_HEADING,
        blocks.BLOCK_PARAGRAPH,
    ]


def test_closing_hashes_and_surrounding_space_are_not_heading_text() -> None:
    assert segment("#  Design  ##")[0].text == "Design"


def test_paragraph_sentences_share_a_group_and_paragraphs_do_not() -> None:
    text = "First fact [1]. Second fact [2].\n\nThird fact [1]."
    assert _shape(text) == [
        ("First fact [1].", blocks.BLOCK_PARAGRAPH, 0, 0, 0),
        ("Second fact [2].", blocks.BLOCK_PARAGRAPH, 0, 0, 0),
        ("Third fact [1].", blocks.BLOCK_PARAGRAPH, 1, 0, 0),
    ]


def test_soft_wrapped_paragraph_lines_join_with_a_space() -> None:
    text = "The study is a single-dose\ncrossover design [1]."
    assert [u.text for u in segment(text)] == ["The study is a single-dose crossover design [1]."]


def test_bullets_become_items_of_one_list_group() -> None:
    text = "- SAC (B/M/E) [1].\n- APSD (B/E) [1]. Second sentence [2].\n* PK BE [3]"
    assert _shape(text) == [
        ("SAC (B/M/E) [1].", blocks.BLOCK_BULLET, 0, 0, 0),
        ("APSD (B/E) [1].", blocks.BLOCK_BULLET, 0, 1, 0),
        ("Second sentence [2].", blocks.BLOCK_BULLET, 0, 1, 0),
        ("PK BE [3]", blocks.BLOCK_BULLET, 0, 2, 0),
    ]


def test_numbered_items_are_a_distinct_kind_and_keep_their_own_group() -> None:
    text = "- a [1].\n\n1. first [1].\n2) second [2]."
    shape = _shape(text)
    assert shape[0][1:3] == (blocks.BLOCK_BULLET, 0)
    assert shape[1] == ("first [1].", blocks.BLOCK_NUMBERED, 1, 0, 0)
    assert shape[2] == ("second [2].", blocks.BLOCK_NUMBERED, 1, 1, 0)


def test_indented_continuation_line_belongs_to_its_list_item() -> None:
    text = "- A long item that wraps\n  onto a second line [1]."
    assert _shape(text) == [
        ("A long item that wraps onto a second line [1].", blocks.BLOCK_BULLET, 0, 0, 0)
    ]


def test_blank_line_ends_a_list_and_the_next_bullets_start_a_new_group() -> None:
    text = "- a [1].\n\n- b [1]."
    assert [(u.block.group, u.block.item) for u in segment(text)] == [(0, 0), (1, 0)]


def test_table_cells_are_units_with_row_column_and_width() -> None:
    text = "| Study | Option I | Option II |\n|---|:---:|---|\n| PK BE | Yes [1] | Yes [2] |\n| SAC |  | Yes [1] |"
    units = segment(text)
    assert [(u.text, u.block.item, u.block.cell) for u in units] == [
        ("Study", 0, 0),
        ("Option I", 0, 1),
        ("Option II", 0, 2),
        ("PK BE", 1, 0),
        ("Yes [1]", 1, 1),
        ("Yes [2]", 1, 2),
        ("SAC", 2, 0),
        # The empty cell emits no unit; the renderer pads it back from width.
        ("Yes [1]", 2, 2),
    ]
    assert {u.block.kind for u in units} == {blocks.BLOCK_TABLE}
    assert {u.block.group for u in units} == {0}
    assert {u.block.width for u in units} == {3}


def test_delimiter_row_is_structure_not_a_claim() -> None:
    assert "---" not in split_units("| a | b |\n|---|---|\n| c | d |")


def test_row_cells_beyond_the_header_width_are_kept_with_their_column() -> None:
    """GFM would drop the excess cell; we keep it (the renderer widens the
    grid) rather than silently discarding model text."""
    units = segment("| a | b |\n|---|---|\n| c | d | e |")
    assert [(u.text, u.block.cell, u.block.width) for u in units] == [
        ("a", 0, 2),
        ("b", 1, 2),
        ("c", 0, 2),
        ("d", 1, 2),
        ("e", 2, 2),
    ]


def test_a_pipe_line_without_a_delimiter_row_is_prose_not_a_table() -> None:
    text = "Some note.\n| not a table, just a pipe |\nMore text [1]."
    units = segment(text)
    assert {u.block.kind for u in units} == {blocks.BLOCK_PARAGRAPH}
    # The pipe line has no terminator, so it reads on into the next sentence.
    assert [u.text for u in units] == [
        "Some note.",
        "| not a table, just a pipe | More text [1].",
    ]


def test_two_pipe_lines_without_a_delimiter_are_prose_too() -> None:
    text = "| a | b |\n| c | d |"
    units = segment(text)
    assert {u.block.kind for u in units} == {blocks.BLOCK_PARAGRAPH}
    assert len(units) == 1  # one paragraph, joined with a space


def test_a_trailing_pipe_line_at_end_of_input_is_released_as_prose() -> None:
    units = segment("Intro.\n| dangling |")
    assert [u.block.kind for u in units] == [blocks.BLOCK_PARAGRAPH] * 2


def test_table_without_trailing_pipes_still_parses() -> None:
    units = segment("| a | b\n|---|---\n| c | d")
    assert [u.text for u in units] == ["a", "b", "c", "d"]


def test_a_table_then_prose_are_separate_groups() -> None:
    text = "| a | b |\n|---|---|\n| c | d |\nAfterwards [1]."
    units = segment(text)
    assert units[-1].text == "Afterwards [1]."
    assert units[-1].block.kind == blocks.BLOCK_PARAGRAPH
    assert units[-1].block.group == 1


def test_blockquote_marker_is_dropped_and_the_text_is_a_paragraph() -> None:
    assert _shape("> Quoted fact [1].") == [("Quoted fact [1].", blocks.BLOCK_PARAGRAPH, 0, 0, 0)]


def test_split_units_is_the_text_projection() -> None:
    text = "## H\n\nA [1]. B [2].\n\n- c [1]."
    assert split_units(text) == ["H", "A [1].", "B [2].", "c [1]."]


def test_empty_and_whitespace_input_yield_nothing() -> None:
    assert segment("") == []
    assert segment("  \n\n  ") == []


def test_default_block_is_the_first_paragraph() -> None:
    assert Block() == Block(kind=blocks.BLOCK_PARAGRAPH, group=0, item=0, cell=0, level=0, width=0)
