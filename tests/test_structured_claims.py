"""Structured claims: markdown shape survives the gate and is rebuilt by the renderer.

Until 2026-08-21 "presentation is a feature" was half-delivered: the gate
stripped a heading's ``#`` so the claim survived, and the renderer then
welded the heading onto the next sentence. These tests pin the other half --
the parser tags every claim with the block it came from, the gate carries
the tag without trusting it, and ``render_answer`` writes real headings,
lists and GFM tables whose cells carry their own validated markers.

INV-1 is unchanged: a heading, a bullet or a cell is still ONE claim, still
needs its own marker to state a source fact, and a bad marker on a heading or
a cell is dropped rather than re-stamped by token overlap.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.common import blocks
from regwatch.eval import metrics
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import prose_turn
from regwatch.generate import turn_gate as tg
from regwatch.generate.llm import LLMResponse
from regwatch.retrieve.retriever import RetrievedPassage
from tests.test_invariants import _meta, _only_route_json, _seed_corpus

pytestmark = pytest.mark.invariants

_QUESTION = "Which BE design does the albuterol sulfate PSG recommend?"
_CORPUS = [("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))]

# The production shape (#2506): a heading, a paragraph, bullets, a matrix.
_STRUCTURED = (
    "## Recommended BE design\n"
    "\n"
    "FDA recommends a single-dose fasting study [1]. A fed study follows the same design [1].\n"
    "\n"
    "- SAC (B/M/E) [1].\n"
    "- APSD (B/E) [1]\n"
    "\n"
    "| Study | Option I | Option II |\n"
    "|---|---|---|\n"
    "| PK BE | Yes [1] | Yes [1] |\n"
    "| PD study |  | Yes [1] |\n"
    "\n"
    "Happy to walk through the charcoal-block arm as well."
)


def _passages(n: int = 1) -> list[RetrievedPassage]:
    return [
        RetrievedPassage(
            chunk_id=f"chunk-{i}",
            text="Fasting BE study with 36 subjects.",
            score=1.0,
            doc_id=1,
            version_id=1,
            page=i,
            section_path=None,
            normalized_name="albuterol sulfate",
            source_url="http://example/x.pdf",
            short_name="PSG_020503",
            metadata={"dosage_form": "aerosol", "route": "inhalation", "psg_type": "draft"},
        )
        for i in range(3, 3 + n)
    ]


def _admit(text: str, passages: list[RetrievedPassage]) -> tg.AdmittedTurn:
    parsed = prose_turn.parse(text, passages=passages, selective=True)
    return tg.admit_claims(
        parsed.turn_type,
        prose_turn.to_claims(parsed, passages),
        passages=passages,
        question=_QUESTION,
        correct=True,
        downgrade_uncited=False,
        kinds=[c.kind for c in parsed.claims],
        selective=True,
    )


# ---------- parser ----------


def test_heading_is_a_separate_claim_not_glued_to_the_paragraph() -> None:
    parsed = prose_turn.parse(_STRUCTURED, passages=_passages(), selective=True)
    heading, first = parsed.claims[0], parsed.claims[1]
    assert heading.text == "Recommended BE design"
    assert heading.block.kind == blocks.BLOCK_HEADING
    assert heading.kind == "conversation"
    assert first.text == "FDA recommends a single-dose fasting study."
    assert first.block.kind == blocks.BLOCK_PARAGRAPH
    assert first.cite_indices == [0]


def test_bullet_and_cell_markers_resolve_without_a_terminal_period() -> None:
    parsed = prose_turn.parse(_STRUCTURED, passages=_passages(), selective=True)
    by_text = {c.text: c for c in parsed.claims}
    assert by_text["APSD (B/E)"].cite_indices == [0]
    assert by_text["APSD (B/E)"].kind == "source_fact"
    assert by_text["APSD (B/E)"].block.kind == blocks.BLOCK_BULLET
    cells = [c for c in parsed.claims if c.block.kind == blocks.BLOCK_TABLE]
    assert [(c.text, c.block.item, c.block.cell, c.cite_indices) for c in cells] == [
        ("Study", 0, 0, []),
        ("Option I", 0, 1, []),
        ("Option II", 0, 2, []),
        ("PK BE", 1, 0, []),
        ("Yes", 1, 1, [0]),
        ("Yes", 1, 2, [0]),
        ("PD study", 2, 0, []),
        ("Yes", 2, 2, [0]),
    ]
    assert parsed.leftover_brackets == []


def test_a_paragraph_sentence_still_needs_its_marker_before_the_period() -> None:
    """F4 is unchanged for prose: the relaxed grammar is for line-terminated
    blocks only. A paragraph tail with no terminator is still a cut-off draft."""
    parsed = prose_turn.parse(
        "A fasting study is recommended [1]", passages=_passages(), selective=True
    )
    assert parsed.claims == []


def test_post_terminator_marker_is_reattached_before_the_block_split() -> None:
    """Review finding: segment() sentence-splits inside a paragraph, so the
    v6 reattachment ("required. [1] Next" -> "required [1]. Next") must run
    on the raw text first or the marker is orphaned onto the next sentence
    and killed as a leftover bracket."""
    text = "A fasting study is required. [1] A fed study also applies [1].\n\n- Bullet item. [1] Second."
    parsed = prose_turn.parse(text, passages=_passages(), selective=True)
    assert parsed.leftover_brackets == []
    assert [(c.text, c.cite_indices) for c in parsed.claims] == [
        ("A fasting study is required.", [0]),
        ("A fed study also applies.", [0]),
        ("Bullet item.", [0]),
        ("Second.", []),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "A fasting study is required. [1]\n## Recommended design\nMore text [1].",
        "A fasting study is required. [1]\n\n- A bullet point [1].",
        "A fasting study is required. [1]\n| Study | Option I |\n|---|---|\n| PK | Yes [1] |",
    ],
)
def test_reattachment_never_eats_the_line_break_before_the_next_block(text: str) -> None:
    """Judge finding on the reattach fix: the old ``\\s*`` after the marker
    group swallowed the newline, welding the heading/bullet/table line back
    into the paragraph -- the exact defect the block layer exists to remove."""
    parsed = prose_turn.parse(text, passages=_passages(), selective=True)
    assert parsed.claims[0].text == "A fasting study is required."
    assert parsed.claims[0].cite_indices == [0]
    assert parsed.claims[1].block.kind != blocks.BLOCK_PARAGRAPH
    assert parsed.leftover_brackets == []


def test_v6_parse_is_unchanged_and_carries_only_the_default_block() -> None:
    parsed = prose_turn.parse(_STRUCTURED, passages=_passages(), selective=False)
    assert {c.block for c in parsed.claims} == {blocks.PARAGRAPH}
    # The legacy weld: heading glued to the sentence below it, exactly as before.
    assert parsed.claims[0].text.startswith("## Recommended BE design FDA recommends")


def test_bounds_do_not_read_a_long_table_as_one_sentence() -> None:
    row = "| " + " | ".join("cell text" for _ in range(8)) + " |\n"
    table = row + "|" + "---|" * 8 + "\n" + row * 90
    assert len(table) > prose_turn.PROSE_MAX_ANSWER_CHARS
    assert prose_turn.bounds_exceeded(table) == "answer_too_long"
    small = row + "|" + "---|" * 8 + "\n" + row * 30
    assert len(small) > prose_turn.PROSE_MAX_SENTENCE_CHARS
    assert prose_turn.bounds_exceeded(small) is None


# ---------- gate + renderer ----------


def test_render_rebuilds_heading_paragraphs_bullets_and_the_matrix() -> None:
    turn = _admit(_STRUCTURED, _passages())
    assert turn.verdict == tg.VERDICT_ANSWER
    assert tg.render_answer(turn) == (
        "### Recommended BE design\n"
        "\n"
        "FDA recommends a single-dose fasting study [PSG_020503, p.3]. "
        "A fed study follows the same design [PSG_020503, p.3].\n"
        "\n"
        "- SAC (B/M/E) [PSG_020503, p.3].\n"
        "- APSD (B/E) [PSG_020503, p.3]\n"
        "\n"
        "| Study | Option I | Option II |\n"
        "| --- | --- | --- |\n"
        "| PK BE | Yes [PSG_020503, p.3] | Yes [PSG_020503, p.3] |\n"
        "| PD study |  | Yes [PSG_020503, p.3] |\n"
        "\n"
        "Happy to walk through the charcoal-block arm as well."
        "\n\nSources:\n- [PSG_020503, p.3]"
    )


def test_a_lead_in_line_ending_in_a_colon_gets_no_extra_period() -> None:
    """Prod #2511 rendered "two BE pathways:." -- the legacy "." was appended
    to a colon-terminated lead-in above a table."""
    text = (
        "The PSG provides two BE pathways:\n\n| Study | Option I |\n|---|---|\n| PK BE | Yes [1] |"
    )
    turn = _admit(text, _passages())
    rendered = tg.render_answer(turn)
    assert rendered.startswith("The PSG provides two BE pathways:\n\n| Study | Option I |")
    assert ":." not in rendered


def test_deep_headings_fold_to_two_registers() -> None:
    turn = _admit("# One\n\n#### Four\n\nA fact [1].", _passages())
    rendered = tg.render_answer(turn)
    assert rendered.startswith("### One\n\n#### Four\n\n")


def test_numbered_list_is_renumbered_after_a_drop() -> None:
    text = "1. First step [1].\n2. A fed study is not required [9].\n3. Third step [1]."
    turn = _admit(text, _passages())
    # The fabricated [9] on a material sentence drops the claim and makes the
    # whole turn a material drop -- the renderer is still exercised directly.
    assert turn.verdict == tg.VERDICT_MATERIAL_DROP
    rendered = tg.render_answer(turn)
    assert "1. First step [PSG_020503, p.3].\n2. Third step [PSG_020503, p.3]." in rendered
    assert "3." not in rendered.split("\n\n")[0]


def test_dropped_cell_leaves_its_slot_empty_and_the_turn_discloses_it() -> None:
    text = "| Study | Option I |\n|---|---|\n| PK BE | Yes [9] |\n| SAC | Yes [1] |"
    turn = _admit(text, _passages())
    assert turn.verdict == tg.VERDICT_PARTIAL
    rendered = tg.render_answer(turn)
    assert "| PK BE |  |" in rendered
    assert "| SAC | Yes [PSG_020503, p.3] |" in rendered
    assert tg.PARTIAL_DROP_DISCLOSURE in rendered


@pytest.mark.parametrize(
    "text",
    [
        "## Study design [9]",
        "| Study | Design |\n|---|---|\n| PK | Study design [9] |",
    ],
)
def test_heading_and_cell_with_a_bad_cite_are_dropped_never_re_stamped(text: str) -> None:
    """The P2 hole, extended: the parser now removes the ``#`` and the pipes
    before the gate sees the text, so the gate's own layout_stripped guard
    cannot fire. The block kind has to carry the ineligibility instead."""
    turn = _admit(text, _passages())
    assert not any(c.correction_method == tg.CORRECTION_LEXICAL for c in turn.admitted)
    assert any(d.reason == tg.DROP_UNKNOWN_CITATION for d in turn.dropped)
    assert all(c.pairs == () for c in turn.admitted)


def test_a_bullet_with_a_bad_cite_is_still_correctable_as_before() -> None:
    """Bullets were always stripped by _sanitize_claim_text and always
    correctable; the block tag must not narrow that."""
    turn = _admit("- Fasting BE study with 36 subjects [9].", _passages())
    assert [c.correction_method for c in turn.admitted] == [tg.CORRECTION_LEXICAL]


def test_heading_that_asserts_a_source_fact_uncited_is_dropped_and_disclosed() -> None:
    """Structure buys no provenance: an uncited deontic heading is an uncited
    source fact and falls to DROP_NO_CITES exactly like a sentence would. The
    VERDICT differs: a heading is not part of the sentence flow, so its loss is
    PARTIAL (hole + disclosure), never a whole-answer material drop. The
    material word is still ledgered on the drop."""
    turn = _admit("## A fed study is not required\n\nA fact [1].", _passages())
    assert turn.verdict == tg.VERDICT_PARTIAL
    assert turn.material_word is None
    assert turn.dropped[0].reason == tg.DROP_NO_CITES
    assert turn.dropped[0].material_word == "not"
    assert turn.dropped[0].block.kind == blocks.BLOCK_HEADING
    rendered = tg.render_answer(turn)
    assert "fed study" not in rendered
    assert tg.PARTIAL_DROP_DISCLOSURE in rendered


def test_the_same_uncited_deontic_sentence_in_a_bullet_still_refuses() -> None:
    """A bullet IS a sentence: the structural leniency does not reach it."""
    turn = _admit("- A fed study is not required\n- A fact [1].", _passages())
    assert turn.verdict == tg.VERDICT_MATERIAL_DROP


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("## Recommended BE design\n\nA fact [1].", "### Recommended BE design"),
        ("## Requirements\n\nA fact [1].", "### Requirements"),
        (
            "| Recommended study | Option I |\n|---|---|\n| PK BE | Yes [1] |",
            "| Recommended study |",
        ),
    ],
)
def test_labels_skip_the_attribution_lexicon(text: str, kept: str) -> None:
    """ "Recommended"/"Requirements" are PSG vocabulary; as a label they report
    nothing. A VALUE cell with the same word is an assertion and still drops."""
    turn = _admit(text, _passages())
    assert turn.verdict == tg.VERDICT_ANSWER
    assert kept in tg.render_answer(turn)


@pytest.mark.parametrize(
    "text",
    [
        # A bare deontic/exemption word in a row label is an assertion.
        "| Study | Notes |\n|---|---|\n| Exempt | see product label [1] |",
        # A full attributed sentence in a row label is an assertion.
        "| Study | Option I |\n|---|---|\n| FDA recommends a fasting study | Yes [1] |",
        "## Waived studies\n\nA fact [1].",
        "## FDA recommends a fasting study\n\nA fact [1].",
    ],
)
def test_label_exemption_covers_topic_words_only(text: str) -> None:
    """Judge finding: is_label is positional, so the exemption had to be
    narrowed to the descriptive vocabulary; "Exempt", "waived" and a verb of
    saying still fire on a label and the claim drops (PARTIAL + disclosure)."""
    turn = _admit(text, _passages())
    assert turn.verdict == tg.VERDICT_PARTIAL
    assert [d.reason for d in turn.dropped] == [tg.DROP_NO_CITES]
    rendered = tg.render_answer(turn)
    assert "Exempt" not in rendered and "recommends" not in rendered and "Waived" not in rendered
    assert tg.PARTIAL_DROP_DISCLOSURE in rendered


def test_value_cell_with_an_attribution_word_uncited_is_dropped() -> None:
    turn = _admit("| Study | Status |\n|---|---|\n| PK BE | Recommended |", _passages())
    assert [d.reason for d in turn.dropped] == [tg.DROP_NO_CITES]
    assert turn.dropped[0].block.cell == 1


def test_placeholder_cells_are_structure_not_claims() -> None:
    text = "| Study | Option I | Option II |\n|---|---|---|\n| PK BE | -- | Yes [1] |\n| SAC | N/A | \u2014 |"
    turn = _admit(text, _passages())
    assert turn.verdict == tg.VERDICT_ANSWER
    assert turn.dropped == ()
    assert "| PK BE |  | Yes [PSG_020503, p.3] |\n| SAC |  |  |" in tg.render_answer(turn)


def test_flat_turn_renders_byte_identically_to_the_old_renderer() -> None:
    turn = _admit("A fasting study is recommended [1]. Happy to help further.", _passages())
    assert tg.render_answer(turn) == (
        "A fasting study is recommended [PSG_020503, p.3]. Happy to help further."
        "\n\nSources:\n- [PSG_020503, p.3]"
    )


def test_ledger_records_the_block_per_claim_under_selective_only() -> None:
    turn = _admit(_STRUCTURED, _passages())
    selective = tg.ledger(
        turn, model="m", prompt_version="7", renderer_version=tg.RENDERER_VERSION_SELECTIVE
    )
    assert selective["claims"][0]["block"] == {
        "kind": blocks.BLOCK_HEADING,
        "group": 0,
        "item": 0,
        "cell": 0,
    }
    cell = next(c for c in selective["claims"] if c["block"]["kind"] == blocks.BLOCK_TABLE)
    assert set(cell["block"]) == {"kind", "group", "item", "cell"}
    legacy = tg.ledger(turn, model="m", prompt_version="6")
    assert all("block" not in c for c in legacy["claims"])


def test_claim_tags_still_zip_against_render_order() -> None:
    turn = _admit(_STRUCTURED, _passages())
    tags = tg.claim_tags(turn)
    assert len(tags) == len(turn.admitted)
    assert sum(1 for t in tags if t.cited) == 7


# ---------- metrics ----------


def test_metrics_count_cells_and_bullets_as_units() -> None:
    rendered = tg.render_answer(_admit(_STRUCTURED, _passages()))
    assert metrics.claim_count(rendered) == 7
    # The heading and the closing sentence are the two uncited units.
    assert metrics.sentence_citation_rate(rendered) == pytest.approx(7 / 14)
    assert metrics._longest_sentence_chars(rendered) < 120


# ---------- end to end ----------


def _stub_llm(text: str) -> Any:
    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    return _LLM()


def _v7_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGWATCH_PROSE_SYNTHESIS", "1")
    monkeypatch.setenv("REGWATCH_SELECTIVE_CITATION", "1")
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()


def test_structured_answer_is_served_with_its_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_STRUCTURED))

    result = qa_mod.ask(_QUESTION)

    assert not result.refused, result.answer
    assert result.answer.startswith("### Recommended BE design\n\nFDA recommends")
    assert "| PK BE | Yes [PSG_020503, p.3] | Yes [PSG_020503, p.3] |" in result.answer
    assert "- APSD (B/E) [PSG_020503, p.3]\n" in result.answer
    assert "[1]" not in result.answer
    turn = _only_route_json()["turn"]
    assert turn["verdict"] == "answer"
    assert turn["renderer_version"] == tg.RENDERER_VERSION_SELECTIVE
    kinds = [c["block"]["kind"] for c in turn["claims"]]
    assert kinds.count(blocks.BLOCK_TABLE) == 8
    assert kinds[0] == blocks.BLOCK_HEADING
