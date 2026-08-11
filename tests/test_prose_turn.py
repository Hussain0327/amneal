"""The prose parser: every rule, and the safe direction each one fails in.

The parser is the ONLY deterministic reading of model prose; nothing it emits
is admitted without the gate. These tests pin the reading itself: which
brackets are citations (trailing only, finding F4), which sentences a marker
binds (its own, never a neighbor), and which uncited sentences a frame may NOT
launder (materiality, finding F3).
"""

from __future__ import annotations

import pytest

from regwatch.generate import prose_turn as pt
from regwatch.retrieve.retriever import RetrievedPassage

pytestmark = pytest.mark.invariants


def _passage(
    short_name: str,
    page: int,
    *,
    chunk_id: str,
    text: str,
) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=chunk_id,
        text=text,
        score=0.71,
        doc_id=1,
        version_id=10,
        page=page,
        section_path=None,
        normalized_name="albuterol sulfate",
        source_url=f"http://example/{short_name}.pdf",
        short_name=short_name,
        metadata={},
    )


# Ordered exactly as they would be sent to the model: marker [1] names the
# first entry, [2] the second.
_PASSAGES = [
    _passage("PSG_020503", 3, chunk_id="chunk-1", text="Fasting single-dose crossover study."),
    _passage("PSG_021730", 4, chunk_id="chunk-2", text="Dissolution: USP paddle."),
]


def _parse(raw: str) -> pt.ParsedProseTurn:
    return pt.parse(raw, passages=_PASSAGES)


# ---------- marker grammar ----------


@pytest.mark.parametrize(
    ("raw", "indices", "markers"),
    [
        ("A fasting crossover study is described [1].", [0], ["1"]),
        ("A fasting crossover study is described [1][2].", [0, 1], ["1", "2"]),
        ("A fasting crossover study is described [1, 2].", [0, 1], ["1", "2"]),
        ("A fasting crossover study is described[1].", [0], ["1"]),
        ("A fasting crossover study is described [1] [2].", [0, 1], ["1", "2"]),
        # A duplicated marker dedupes the resolved index but stays visible in
        # the raw declaration.
        ("A fasting crossover study is described [1][1].", [0], ["1", "1"]),
    ],
)
def test_trailing_marker_grammar(raw: str, indices: list[int], markers: list[str]) -> None:
    turn = _parse(raw)
    assert turn.turn_type == "ANSWER"
    assert [c.kind for c in turn.claims] == ["source_fact"]
    claim = turn.claims[0]
    assert claim.text == "A fasting crossover study is described."
    assert claim.cite_indices == indices
    assert claim.raw_markers == markers
    assert turn.leftover_brackets == []


# ---------- position rule (finding F4) ----------


@pytest.mark.parametrize(
    "raw",
    [
        # A passage-echo quote carrying a numeric bracket mid-sentence.
        'The passage header text "[1] Dissolution" appears in the corpus.',
        # A user-typed bracket reaching the model via the question.
        "You asked about item [2] of the checklist.",
        # A marker at the START of a sentence is not trailing either.
        "[1] The paddle method is specified.",
    ],
)
def test_non_trailing_bracket_kills_its_sentence(raw: str) -> None:
    """A bracket that is not sentence-trailing is NEVER consumed as a citation.

    An in-range [1] quoted from source or user text would otherwise resolve as
    a valid-but-wrong citation; the safe direction is to drop the sentence.
    """
    turn = _parse(raw)
    assert turn.claims == []
    assert turn.leftover_brackets == [raw]


def test_mid_sentence_bracket_kills_even_beside_a_valid_trailing_marker() -> None:
    raw = 'The echoed header "[1] Dissolution" is discussed here [2].'
    turn = _parse(raw)
    # The trailing [2] dies WITH its sentence; it is not salvaged as a cite.
    assert turn.claims == []
    assert turn.leftover_brackets == [raw]


# ---------- sentence-initial marker reattachment ----------


def test_marker_after_terminator_reattaches_to_its_own_sentence() -> None:
    turn = _parse("A fasting crossover study is described. [1] The paddle method is specified. [2]")
    assert [(c.text, c.cite_indices) for c in turn.claims] == [
        ("A fasting crossover study is described.", [0]),
        ("The paddle method is specified.", [1]),
    ]
    assert turn.leftover_brackets == []
    assert turn.truncated_material is False


# ---------- pair-grammar collision ----------


def test_verbatim_pair_echo_normalizes_to_its_passage_index() -> None:
    turn = _parse("The paddle method is specified [PSG_021730, p.4].")
    claim = turn.claims[0]
    assert claim.kind == "source_fact"
    assert claim.cite_indices == [1]
    assert claim.raw_markers == ["PSG_021730, p.4"]


def test_pair_echo_resolves_case_insensitively_but_keeps_the_raw_echo() -> None:
    turn = _parse("The paddle method is specified [psg_021730, p.4].")
    claim = turn.claims[0]
    assert claim.cite_indices == [1]
    assert claim.raw_markers == ["psg_021730, p.4"]


def test_compound_pair_echo_resolves_every_pair() -> None:
    turn = _parse("Both design and method are covered [PSG_020503, p.3; PSG_021730, p.4].")
    assert turn.claims[0].cite_indices == [0, 1]


def test_fabricated_pair_kills_its_sentence() -> None:
    raw = "The waiver criteria appear at [PSG_999999, p.9]."
    turn = _parse(raw)
    assert turn.claims == []
    assert turn.leftover_brackets == [raw]


# ---------- unknown (out-of-range) markers ----------


def test_out_of_range_marker_is_carried_as_declared_but_unresolvable() -> None:
    """The gate, not the parser, decides whether to drop or correct it."""
    turn = _parse("The pilot study design is described [7].")
    claim = turn.claims[0]
    assert claim.kind == "source_fact"
    assert claim.cite_indices == []
    assert claim.raw_markers == ["7"]


def test_mixed_in_and_out_of_range_markers_keep_both_declarations() -> None:
    turn = _parse("The pilot study design is described [1][7].")
    claim = turn.claims[0]
    assert claim.cite_indices == [0]
    assert claim.raw_markers == ["1", "7"]


# ---------- marker scope ----------


def test_one_trailing_marker_never_covers_a_neighboring_sentence() -> None:
    turn = _parse(
        "The two documents cover the same product. "
        "The paddle method appears in the second document [2]."
    )
    first, second = turn.claims
    assert (first.kind, first.cite_indices) == ("conversation", [])
    assert (second.kind, second.cite_indices) == ("source_fact", [1])


# ---------- epistemic classification ----------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("My reading is that the two documents describe the same design.", "reasoning"),
        # Frame matching is case- and whitespace-normalized.
        ("MY  READING   IS that the two documents align.", "reasoning"),
        ("Beyond the guidance, sponsors add a pilot study.", "reasoning"),
        ("Reading the guidance together, the design language matches.", "reasoning"),
        ("Let me know if you want more detail.", "conversation"),
        # FRAMED MATERIALITY (finding F3): a model-authored frame must not
        # launder a material FDA claim -- the hit reclassifies to source_fact
        # with zero cites, the gate's correct-or-drop path.
        ("My reading is that a fed study is not required.", "source_fact"),
        # The same guard on the conversation residual (AIS guard).
        ("A fed study must accompany the submission.", "source_fact"),
    ],
)
def test_uncited_sentence_classification(raw: str, kind: str) -> None:
    turn = _parse(raw)
    claim = turn.claims[0]
    assert claim.kind == kind
    assert claim.cite_indices == []
    assert claim.raw_markers == []


# ---------- v7 selective classification (B.10.3.3), flag-gated ----------
# v6's table above is untouched by any of this: selective=True is the only way
# to reach _classify_uncited_selective, so the v6 path stays byte-identical.


def _parse_selective(raw: str) -> pt.ParsedProseTurn:
    return pt.parse(raw, passages=_PASSAGES, selective=True)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        # AIS guard (P1): an attribution verb with no obligation word still
        # reports what a source SAYS -- selective mode must not serve this
        # uncited, unlike the v6 table's bald materiality-only residual.
        ("FDA recommends a fasting study.", "source_fact"),
        ("A clean conversational sentence with friendly chatter.", "conversation"),
        # Framed and clean on BOTH lexicons -> the uncited reasoning channel.
        ("My reading is that the two designs match.", "reasoning"),
        # Framed but the BODY carries a materiality word -- the frame is
        # content-free hedge text and must not launder it (F3/P0).
        ("My reading is that a fed study is not required.", "source_fact"),
        # Framed but the BODY carries a source-assertion word.
        ("Beyond the guidance, FDA recommends a fed study.", "source_fact"),
    ],
)
def test_selective_uncited_sentence_classification(raw: str, kind: str) -> None:
    turn = _parse_selective(raw)
    claim = turn.claims[0]
    assert claim.kind == kind
    assert claim.cite_indices == []
    assert claim.raw_markers == []


# ---------- frame_split (moved to turn_gate, B.10.3.2; re-exported here) ----------


def test_frame_split_unframed_returns_original_text() -> None:
    from regwatch.generate.turn_gate import frame_split

    assert frame_split("Let me know if you want more detail.") == (
        "",
        "Let me know if you want more detail.",
    )


def test_frame_split_strips_the_matched_prefix_and_leading_punctuation() -> None:
    from regwatch.generate.turn_gate import frame_split

    assert frame_split("My reading is that the two documents align.") == (
        "my reading is",
        "that the two documents align.",
    )
    # A trailing comma already inside the frame prefix leaves no extra
    # punctuation for the body strip to remove.
    assert frame_split("Beyond the guidance, sponsors add a pilot study.") == (
        "beyond the guidance,",
        "sponsors add a pilot study.",
    )
    # The semicolon joining the two-clause frame is exactly what the leading
    # ;/,/: strip exists for.
    assert frame_split(
        "The guidance does not state this directly; my reading is that forms differ."
    ) == ("the guidance does not state this directly", "my reading is that forms differ.")


def test_frame_split_is_whitespace_and_case_normalized() -> None:
    from regwatch.generate.turn_gate import frame_split

    prefix, body = frame_split("  MY   READING    IS   that combos match.  ")
    assert prefix == "my reading is"
    assert body == "that combos match."


def test_prose_turn_reexports_the_same_frame_prefixes_object() -> None:
    """prose_turn.REASONING_FRAME_PREFIXES must stay a valid attribute for every
    existing reference after the B.10.3.2 move to turn_gate."""
    from regwatch.generate import turn_gate as tg

    assert pt.REASONING_FRAME_PREFIXES is tg.REASONING_FRAME_PREFIXES


# ---------- truncation rule ----------


def test_material_truncated_tail_is_dropped_and_flagged() -> None:
    turn = _parse("The paddle method is specified [1]. A fed study must not be")
    assert [c.text for c in turn.claims] == ["The paddle method is specified."]
    assert turn.truncated_material is True


def test_benign_truncated_tail_is_dropped_without_the_flag() -> None:
    # "noting" must not trip the word-boundary "not" match.
    turn = _parse("The paddle method is specified [1]. It is worth noting tha")
    assert [c.text for c in turn.claims] == ["The paddle method is specified."]
    assert turn.truncated_material is False


def test_lone_unterminated_sentence_yields_zero_claims() -> None:
    turn = _parse("A fed study must not be")
    assert turn.claims == []
    assert turn.truncated_material is True


# ---------- NO_EVIDENCE sentinel ----------


def test_sentinel_completion_is_a_no_evidence_turn() -> None:
    for raw in (pt.PROSE_NO_EVIDENCE_SENTINEL, "  NO_EVIDENCE. \n"):
        turn = _parse(raw)
        assert turn.turn_type == "NO_EVIDENCE"
        assert turn.claims == []
        assert turn.truncated_material is False
        assert turn.leftover_brackets == []


@pytest.mark.parametrize(
    "raw",
    [
        # The sentinel is a whole-completion match, not a substring trigger.
        "NO_EVIDENCE. The paddle method is specified [1].",
        # And it is exact: a lowercase echo is just prose.
        "no_evidence.",
    ],
)
def test_non_sentinel_completions_stay_answers(raw: str) -> None:
    assert _parse(raw).turn_type == "ANSWER"


# ---------- leftover-bracket kill ----------


@pytest.mark.parametrize(
    "raw",
    [
        # A trailing bracket that is neither numeric nor pair-shaped.
        "The appendix holds the details [see appendix].",
        # A bracket with no sentence around it.
        "[1].",
    ],
)
def test_non_citation_brackets_kill_their_sentence(raw: str) -> None:
    turn = _parse(raw)
    assert turn.claims == []
    assert turn.leftover_brackets == [raw]


def test_kill_records_the_full_sentence_so_materiality_stays_checkable() -> None:
    """The caller must be able to ask whether a killed sentence was material."""
    raw = "A fed study is not required [see note]."
    turn = _parse(raw)
    assert turn.leftover_brackets == [raw]


def test_empty_completion_parses_to_an_empty_answer() -> None:
    turn = _parse("")
    assert turn.turn_type == "ANSWER"
    assert turn.claims == []
    assert turn.truncated_material is False
    assert turn.leftover_brackets == []


# --- Pathological-output bounds (issue #183) --------------------------------
#
# Since #183 the prose arms apply NO length bound of any kind: turn_gate's
# admission ladder never inspects length, and the v5 schema's caps are
# unreachable from this path by design. These bounds exist to stop a degenerate
# completion (a repetition loop, a model that never terminates a sentence), not
# to constrain how a real answer is written. The measured v7 gold run had a
# longest sentence of 488 chars and a longest answer of 1,823, so both bounds
# sit roughly 4x above anything real, and neither fired on any of the 62 rows.


def test_a_normal_cited_answer_breaches_no_bound() -> None:
    assert pt.bounds_exceeded("A fasting study is recommended [1]. It is single-dose [1].") is None


def test_a_sentence_at_the_cap_is_allowed() -> None:
    """The cap is a maximum, not a limit one short of it."""
    assert pt.bounds_exceeded("x" * (pt.PROSE_MAX_SENTENCE_CHARS - 1) + ".") is None


def test_a_sentence_over_the_cap_is_reported() -> None:
    answer = "y" * (pt.PROSE_MAX_SENTENCE_CHARS + 1) + "."
    assert pt.bounds_exceeded(answer) == "sentence_too_long"


def test_a_real_length_enumeration_sentence_is_untouched() -> None:
    """The audit-#1604 shape: long, valid, and the whole point of #183.

    A sentence of this length is exactly what the old 400-char claims-JSON cap
    killed. If this ever trips a bound again, the bound is wrong.
    """
    sentence = (
        "The guidance lists three recommended study options for albuterol "
        "sulfate inhalation aerosol: (I) three in-vitro bioequivalence studies "
        "and one in-vivo bioequivalence study with pharmacokinetic endpoints; "
        "(II) three in-vitro bioequivalence studies with a comparative "
        "clinical endpoint study establishing local delivery equivalence; and "
        "(III) three in-vitro bioequivalence studies alone, where the "
        "applicant justifies equivalence by an alternative route acceptable "
        "to the agency [1]."
    )
    # The band the 400-char claims-JSON cap actually killed in production.
    assert 400 < len(sentence) < 600
    assert pt.bounds_exceeded(sentence) is None


def test_an_answer_over_the_total_ceiling_is_reported() -> None:
    """Many individually-legal sentences still must not run away."""
    one = "A recommended study is described here [1]. "
    answer = one * (pt.PROSE_MAX_ANSWER_CHARS // len(one) + 2)
    assert len(answer) > pt.PROSE_MAX_ANSWER_CHARS
    assert pt.bounds_exceeded(answer) == "answer_too_long"


def test_the_sentence_bound_is_reported_before_the_total_bound() -> None:
    """One repair instruction has to name one fault; the sentence is specific."""
    answer = "z" * (pt.PROSE_MAX_SENTENCE_CHARS + 1) + ". " + "w" * pt.PROSE_MAX_ANSWER_CHARS + "."
    assert pt.bounds_exceeded(answer) == "sentence_too_long"


def test_the_bounds_sit_far_above_measured_real_output() -> None:
    """Pins the headroom the numbers were chosen for, not just the numbers.

    Measured on the 62-row v7 gold run: longest sentence 488, longest answer
    1,823. If someone tightens a bound toward real output, this fails.
    """
    assert pt.PROSE_MAX_SENTENCE_CHARS >= 4 * 488
    assert pt.PROSE_MAX_ANSWER_CHARS >= 4 * 1823
