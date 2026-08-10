"""The claim admission gate: every rule, and what breaks if it is removed.

This is the reliability boundary the whole change exists for. Each test here
fails if the corresponding rule is deleted, so the file doubles as the argument
for why replacing the prose segment gate is safe rather than merely different.
"""

from __future__ import annotations

import json

import pytest

from regwatch.eval.metrics import faithfulness
from regwatch.generate import turn_gate as tg
from regwatch.generate.rag_contract import ClaimTag
from regwatch.retrieve.retriever import RetrievedPassage
from tests.conftest import synth_turn_json

pytestmark = pytest.mark.invariants


def _passage(
    short_name: str = "PSG_020503",
    page: int = 3,
    *,
    chunk_id: str = "chunk-1",
    score: float = 0.71,
    text: str = "Fasting single-dose two-way crossover bioequivalence study in healthy subjects.",
    normalized_name: str = "albuterol sulfate",
    metadata: dict[str, str] | None = None,
) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=chunk_id,
        text=text,
        score=score,
        doc_id=1,
        version_id=10,
        page=page,
        section_path=None,
        normalized_name=normalized_name,
        source_url=f"http://example/{short_name}.pdf",
        short_name=short_name,
        metadata={} if metadata is None else metadata,
    )


_PASSAGES = [_passage(), _passage(page=4, chunk_id="chunk-2", text="Dissolution: USP paddle.")]
_QUESTION = "What study design and dissolution method are recommended?"


def _admit(
    raw: str,
    passages: list[RetrievedPassage] | None = None,
    *,
    correct: bool = False,
) -> tg.AdmittedTurn:
    out = tg.admit_turn(
        raw,
        passages=_PASSAGES if passages is None else passages,
        question=_QUESTION,
        correct=correct,
    )
    assert isinstance(out, tg.AdmittedTurn), out
    return out


# ---------- the regression this whole change exists for ----------


def test_markdown_header_claim_is_dropped_and_does_not_refuse_the_turn() -> None:
    """A header-shaped claim slot costs ONE claim, never the turn.

    The old gate split prose on newlines and refused the whole answer when any
    segment lacked a marker, so a single '## Study design' line refused an
    otherwise correct, correctly-cited answer.
    """
    turn = _admit(
        synth_turn_json(
            [
                ("## Study design", [("PSG_020503", 3)]),
                ("A fasting two-way crossover study is recommended", [("PSG_020503", 3)]),
            ]
        )
    )

    assert turn.verdict == tg.VERDICT_PARTIAL
    assert [c.reason for c in turn.dropped] == [tg.DROP_MARKUP]
    answer = tg.render_answer(turn)
    assert "fasting two-way crossover" in answer
    assert "##" not in answer


def test_trailing_bibliography_placement_is_no_longer_a_refusal() -> None:
    """The exact prod failure: correct content, citations declared separately.

    Under the old gate each content sentence read as uncited because the model
    put its markers at the end. Here placement is not the model's job at all.
    """
    turn = _admit(
        synth_turn_json(
            [
                ("The guidance recommends a fasting two-way crossover study", [("PSG_020503", 3)]),
                ("The dissolution method is the USP paddle", [("PSG_020503", 4)]),
            ]
        )
    )

    assert turn.verdict == tg.VERDICT_ANSWER
    assert not turn.dropped
    answer = tg.render_answer(turn)
    assert "[PSG_020503, p.3]" in answer
    assert "[PSG_020503, p.4]" in answer


# ---------- anti-laundering ----------


def test_two_sentence_claim_slot_is_dropped() -> None:
    """One text slot may not hold a cited fact AND an uncited fabrication.

    Without this rule the claims design would be strictly WEAKER than the
    segment splitter it replaces.
    """
    turn = _admit(
        synth_turn_json(
            [
                (
                    "A fasting study is recommended. A fed study is also recommended.",
                    [("PSG_020503", 3)],
                )
            ]
        )
    )

    assert [c.reason for c in turn.dropped] == [tg.DROP_MULTI_SENTENCE]
    assert turn.verdict == tg.VERDICT_NO_VALID_CITATIONS


def test_unbalanced_fabricated_marker_drops_its_claim() -> None:
    """'[PSG_999999, p.1' survives strip_all_citations (the bracket grammar
    needs a matched pair), so it would otherwise render as literal text sitting
    beside a real citation stamp."""
    turn = _admit(
        synth_turn_json([("A fasting study is recommended [PSG_999999, p.1", [("PSG_020503", 3)])])
    )

    assert [c.reason for c in turn.dropped] == [tg.DROP_MARKUP]
    assert "PSG_999999" not in tg.render_answer(turn)


def test_model_authored_marker_is_stripped_not_trusted() -> None:
    """A balanced marker the model wrote is removed; the renderer writes the
    marker, from the passage, exactly once."""
    turn = _admit(
        synth_turn_json([("A fasting study is recommended [PSG_020503, p.4]", [("PSG_020503", 3)])])
    )

    answer = tg.render_answer(turn)
    assert turn.verdict == tg.VERDICT_ANSWER
    assert answer.count("[PSG_020503, p.3]") == 2  # inline + Sources trailer
    assert "p.4" not in answer


@pytest.mark.parametrize(
    "text",
    [
        "See the guidance at https://example.invalid/psg",
        "See [the guidance](https://example.invalid/psg)",
        "www.example.invalid lists the recommended study",
    ],
)
def test_link_or_bare_url_in_claim_text_is_dropped(text: str) -> None:
    """The answer renders through a markdown component with GFM autolinking, so
    a URL in a claim slot becomes a clickable off-corpus pointer beside real
    citation chips."""
    turn = _admit(synth_turn_json([(text, [("PSG_020503", 3)])]))

    assert [c.reason for c in turn.dropped] == [tg.DROP_MARKUP]


def test_claim_without_cites_is_dropped() -> None:
    turn = _admit(synth_turn_json([("A fed study is also recommended", [])]))

    assert [c.reason for c in turn.dropped] == [tg.DROP_NO_CITES]
    assert turn.verdict == tg.VERDICT_NO_VALID_CITATIONS


# ---------- OD-4: whole-claim drop, then materiality ----------


def test_fabricated_cite_drops_its_claim_while_siblings_survive() -> None:
    turn = _admit(
        synth_turn_json(
            [
                ("A fasting two-way crossover study is recommended", [("PSG_020503", 3)]),
                ("The agency also recommends an in vivo fed study", [("PSG_999999", 7)]),
            ]
        )
    )

    assert turn.verdict == tg.VERDICT_PARTIAL
    assert [c.reason for c in turn.dropped] == [tg.DROP_UNKNOWN_CITATION]
    assert turn.dropped[0].bad_cites == (("PSG_999999", 7),)
    answer = tg.render_answer(turn)
    assert "PSG_999999" not in answer
    assert "fed study" not in answer


def test_one_bad_cite_drops_the_WHOLE_claim_not_just_the_pair() -> None:
    """OD-4. Keeping the sentence with only its valid cite would re-stamp model
    text whose real source was never retrieved onto an unrelated real passage."""
    turn = _admit(
        synth_turn_json(
            [
                (
                    "Single actuation content is measured the same way",
                    [("PSG_020503", 3), ("PSG_999999", 2)],
                )
            ]
        )
    )

    assert [c.reason for c in turn.dropped] == [tg.DROP_UNKNOWN_CITATION]
    assert turn.verdict == tg.VERDICT_NO_VALID_CITATIONS
    assert tg.citations(turn) == []


def test_material_drop_rejects_the_whole_answer() -> None:
    """Dropping a qualifier can invert what remains, so the answer is refused
    rather than served with the exception silently deleted."""
    turn = _admit(
        synth_turn_json(
            [
                ("A fasting two-way crossover study is recommended", [("PSG_020503", 3)]),
                ("A fed study is not required for the 45 mcg strength", [("PSG_999999", 7)]),
            ]
        )
    )

    assert turn.verdict == tg.VERDICT_MATERIAL_DROP
    assert turn.material_word == "not"


def test_immaterial_drop_renders_with_the_user_facing_disclosure() -> None:
    """OD-5: the reader is told something was removed, in plain language, with
    no implementation detail."""
    turn = _admit(
        synth_turn_json(
            [
                ("A fasting two-way crossover study is recommended", [("PSG_020503", 3)]),
                ("The reference product is a metered aerosol", [("PSG_999999", 7)]),
            ]
        )
    )

    answer = tg.render_answer(turn)
    assert turn.verdict == tg.VERDICT_PARTIAL
    assert tg.PARTIAL_DROP_DISCLOSURE in answer
    for leak in ("citation", "claim", "validation", "PSG_999999"):
        assert leak not in answer.split("Sources:")[0].replace(tg.PARTIAL_DROP_DISCLOSURE, ""), leak


def test_full_answer_carries_no_disclosure() -> None:
    turn = _admit(synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])]))

    assert tg.PARTIAL_DROP_DISCLOSURE not in tg.render_answer(turn)


# ---------- the materiality predicate itself ----------


@pytest.mark.parametrize("word", tg.MATERIALITY_WORDS)
def test_every_materiality_word_fires(word: str) -> None:
    assert tg.materiality_trigger(f"The sponsor {word} something here") == word


@pytest.mark.parametrize(
    "text",
    [
        "Mayo Clinic published the reference standard",  # not "may"
        "The notice was published in the Federal Register",  # not "not"
        "Approvedly and mustard are substrings, nothing else here",  # substring-only guard
        "Requirements are described in the appendix",  # not "required"
        "",
    ],
)
def test_materiality_never_fires_on_a_substring(text: str) -> None:
    assert tg.materiality_trigger(text) is None


def test_materiality_is_case_insensitive_and_returns_the_canonical_word() -> None:
    assert tg.materiality_trigger("MUST be fasting") == "must"
    assert tg.materiality_trigger("Only the 90 mcg strength") == "only"


def test_materiality_matches_across_a_hyphen_boundary() -> None:
    """A hyphen is a word boundary, so "non-approved" still trips the guard --
    the safe direction for a predicate whose job is to over-refuse."""
    assert tg.materiality_trigger("The strength is non-approved") == "approved"


# ---------- zero admitted is a refusal, never a no-evidence turn ----------


def test_zero_admitted_claims_is_no_valid_citations_not_no_evidence() -> None:
    """Telling the user the corpus does not cover the question, when the truth
    is that every citation failed validation, is a false statement AND it hides
    a model-quality regression from the refused-rate rollup."""
    turn = _admit(synth_turn_json([("A fasting study is recommended", [("PSG_999999", 1)])]))

    assert turn.verdict == tg.VERDICT_NO_VALID_CITATIONS
    assert turn.turn_type == "ANSWER"


def test_answer_with_zero_claims_is_the_same_verdict() -> None:
    turn = _admit(synth_turn_json([]))

    assert turn.verdict == tg.VERDICT_NO_VALID_CITATIONS
    assert turn.emitted == 0


def test_no_evidence_discards_any_claims_the_model_smuggled_in() -> None:
    turn = _admit(
        synth_turn_json(
            [("A fasting study is recommended", [("PSG_020503", 3)])],
            turn_type="NO_EVIDENCE",
            unsupported=("study design",),
        )
    )

    assert turn.verdict == tg.VERDICT_NO_EVIDENCE
    assert turn.admitted == ()
    assert turn.unsupported == ()
    assert tg.render_answer(turn) == ""


# ---------- unsupported labels are not a second prose channel ----------


@pytest.mark.parametrize(
    "label",
    [
        "waiver is approved for the 45 mcg strength; see p.6",  # punctuation charset
        "bioavailability",  # not anchored in the question
        "",
    ],
)
def test_unsupported_label_failing_a_guard_is_dropped(label: str) -> None:
    turn = _admit(
        synth_turn_json(
            [("A fasting study is recommended", [("PSG_020503", 3)])],
            unsupported=(label,),
        )
    )

    assert turn.unsupported == ()
    assert tg.PARTIAL_EVIDENCE_PREFIX not in tg.render_answer(turn)


def test_anchored_unsupported_label_is_kept_and_rendered_last() -> None:
    turn = _admit(
        synth_turn_json(
            [("A fasting study is recommended", [("PSG_020503", 3)])],
            unsupported=("dissolution method",),
        )
    )

    answer = tg.render_answer(turn)
    assert turn.unsupported == ("dissolution method",)
    body = answer.split("\n\nSources:")[0]
    assert body.strip().endswith(f"{tg.PARTIAL_EVIDENCE_PREFIX} dissolution method.")


# ---------- rendering contract ----------


def test_markers_are_canonical_uppercase_from_the_passage() -> None:
    turn = _admit(synth_turn_json([("A fasting study is recommended", [("psg_020503", 3)])]))

    assert "[PSG_020503, p.3]" in tg.render_answer(turn)
    assert "psg_020503" not in tg.render_answer(turn)


def test_every_rendered_pair_is_present_in_the_citations_list() -> None:
    turn = _admit(
        synth_turn_json(
            [
                ("A fasting study is recommended", [("PSG_020503", 3)]),
                ("The dissolution method is the USP paddle", [("PSG_020503", 4)]),
            ]
        )
    )

    from regwatch.common.citations import iter_psg_citations

    rendered = {(s.upper(), p) for s, p in iter_psg_citations(tg.render_answer(turn))}
    listed = {(c.short_name.upper(), c.page) for c in tg.citations(turn)}
    assert rendered == listed == {("PSG_020503", 3), ("PSG_020503", 4)}


def test_faithfulness_is_one_for_an_all_admitted_turn() -> None:
    """The renderer emits no uncited connective prose -- which is what makes the
    eval's faithfulness pin hold by construction rather than by luck."""
    turn = _admit(
        synth_turn_json(
            [
                ("A fasting two-way crossover study is recommended", [("PSG_020503", 3)]),
                ("The dissolution method is the USP paddle", [("PSG_020503", 4)]),
            ]
        )
    )

    assert faithfulness(tg.render_answer(turn)) == 1.0


def test_claim_tags_mirror_admitted_order() -> None:
    """tg.claim_tags is a pure per-claim accessor: kind + whether a marker
    survived. Every admitted claim in the current (non-selective) gate is a
    cited source_fact -- DROP_NO_CITES removes anything else before it can be
    admitted -- so this also pins the "by construction" half of PR8's
    coincidence proof (A.3)."""
    turn = _admit(
        synth_turn_json(
            [
                ("A fasting study is recommended", [("PSG_020503", 3)]),
                ("The dissolution method is the USP paddle", [("PSG_020503", 4)]),
            ]
        )
    )

    assert tg.claim_tags(turn) == (
        ClaimTag(kind="source_fact", cited=True),
        ClaimTag(kind="source_fact", cited=True),
    )


def test_citation_binds_the_top_ranked_chunk_for_a_shared_page() -> None:
    """Passages arrive best-first; the citation must carry the TOP-ranked
    chunk's id/snippet/score, not the weakest one listed last."""
    best = _passage(chunk_id="chunk-best", score=0.71, text="Dissolution: USP paddle at 50 rpm.")
    worse = _passage(chunk_id="chunk-worse", score=0.34, text="Table 2 footnote.")

    allowed = tg.allowed_passage_map([best, worse])
    assert allowed[("PSG_020503", 3)].chunk_id == "chunk-best"

    turn = _admit(
        synth_turn_json([("The dissolution method is the USP paddle", [("PSG_020503", 3)])]),
        passages=[best, worse],
    )
    citation = tg.citations(turn)[0]
    assert citation.chunk_id == "chunk-best"
    assert citation.score == 0.71
    assert "USP paddle" in citation.snippet


def test_sources_trailer_is_parseable_by_the_shared_grammar() -> None:
    """Conversation memory, faithfulness, and the historical-answer readers all
    split on this exact trailer shape."""
    from regwatch.common.citations import strip_sources_trailer

    turn = _admit(synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])]))
    answer = tg.render_answer(turn)

    assert "\n\nSources:\n- [PSG_020503, p.3]" in answer
    assert "Sources:" not in strip_sources_trailer(answer)


# ---------- parse failures ----------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "The guidance recommends a fasting study.",  # prose, no braces
        '{"turn_type": "ANSWER", "claims": [',  # truncated mid-object
        '{"turn_type": "MAYBE", "claims": []}',  # not in the enum
        '{"turn_type": "ANSWER", "claims": [], "notes": "extra channel"}',  # extra=forbid
    ],
)
def test_unparseable_payloads_are_gate_failures(raw: str) -> None:
    assert isinstance(tg.admit_turn(raw, passages=_PASSAGES, question=_QUESTION), tg.GateFailure)


def test_truncated_json_is_never_repaired_into_an_inverted_claim() -> None:
    """The single most important divergence from the deficiency ladder: repair
    would CLOSE a string cut mid-sentence, and the resulting inverted statement
    would wear a real, clickable citation stamp."""
    truncated = (
        '{"turn_type":"ANSWER","claims":[{"text":"A biowaiver is not granted '
        "for the 45 mcg strength unless in vitro data"
    )

    result = tg.admit_turn(truncated, passages=_PASSAGES, question=_QUESTION)

    assert isinstance(result, tg.GateFailure)
    assert result.reason == "malformed_structure"


def test_fenced_json_is_accepted() -> None:
    payload = synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])])
    turn = _admit(f"```json\n{payload}\n```")

    assert turn.verdict == tg.VERDICT_ANSWER


# ---------- the ledger ----------


def test_ledger_records_the_draft_the_gate_rejected() -> None:
    """The forensic point of the whole change: today an uncited draft is
    replaced by the refusal text and recorded nowhere."""
    turn = _admit(
        synth_turn_json(
            [
                ("A fasting two-way crossover study is recommended", [("PSG_020503", 3)]),
                ("The reference product is a metered aerosol", [("PSG_999999", 7)]),
            ]
        )
    )

    led = tg.ledger(turn, model="stub-model", prompt_version="3")

    assert led["emitted"] == 2
    assert led["admitted"] == 1
    assert led["dropped"] == 1
    assert led["verdict"] == tg.VERDICT_PARTIAL
    assert led["model"] == "stub-model"
    assert led["prompt_version"] == "3"
    dropped = next(c for c in led["claims"] if not c["admitted"])
    assert dropped["index"] == 1
    assert dropped["drop_reason"] == tg.DROP_UNKNOWN_CITATION
    assert dropped["bad_cites"] == ["PSG_999999,p.7"]
    assert "metered aerosol" in dropped["text_prefix"]
    kept = next(c for c in led["claims"] if c["admitted"])
    assert kept["passage_overlap"] > 0
    # JSON-serializable: it is persisted verbatim inside route_json.
    assert json.loads(json.dumps(led))["verdict"] == tg.VERDICT_PARTIAL


def test_ledger_records_the_whole_claim_not_a_window_of_it() -> None:
    """A material_drop must stay investigable six weeks later (INV-6).

    The ledger used to store text[:200] while Claim.text allows 400, so a drop
    could name the materiality word in `material_word` and truncate away the
    clause that contained it -- recording THAT something material was dropped
    but not WHAT, which is the single question the ledger exists to answer.
    The prefix is asserted against the schema cap so the two cannot drift.
    """
    from regwatch.generate.turn_schema import Claim

    schema_cap = Claim.model_fields["text"].metadata[0].max_length
    # One sentence, no markup, > 200 chars, ending in a materiality word so the
    # tail is exactly the part an investigation would need.
    long_claim = (
        "The applicant must conduct a single-dose fasting two-way crossover study "
        "comparing the test product against the reference listed drug under the "
        "conditions described in the guidance, and a fed study is also required "
        "unless a waiver applies"
    )
    assert 200 < len(long_claim) <= schema_cap

    turn = _admit(synth_turn_json([(long_claim, [("PSG_020503", 3)])]))
    led = tg.ledger(turn, model="stub-model", prompt_version="4")

    recorded = led["claims"][0]["text_prefix"]
    assert recorded == long_claim, "the full claim must survive into the ledger"
    assert recorded.endswith("unless a waiver applies")
    assert schema_cap == tg._LEDGER_TEXT_CHARS


# ---------- the schema caps are whole-turn kill switches ----------
#
# Exceeding one is NOT a trim. pydantic raises, admit_turn returns GateFailure,
# and grounded_qa serves "service temporarily unavailable" with ZERO claims. So
# a cap set below what the model may reasonably emit converts a long answer
# into an outage -- which is the argument for raising it, not for keeping it
# low. INV-1 is unaffected either way: the gate validates each claim
# independently, so a longer list cannot make any one claim likelier to survive
# incorrectly.


def _n_claims(n: int) -> str:
    return synth_turn_json(
        [
            (f"Requirement number {i} is stated in the guidance", [("PSG_020503", 3)])
            for i in range(n)
        ]
    )


def test_twenty_claims_are_admitted_at_the_new_cap() -> None:
    turn = _admit(_n_claims(20))

    assert turn.verdict == tg.VERDICT_ANSWER
    assert len(turn.admitted) == 20
    assert not turn.dropped


def test_the_twenty_first_claim_kills_the_whole_turn() -> None:
    """Pins the cap as a HARD WALL, never a silent trim.

    If this ever starts returning an AdmittedTurn with 20 claims, the model's
    21st claim is being discarded without anyone being told -- a silent
    truncation of a regulatory answer, which is worse than the outage.
    """
    out = tg.admit_turn(_n_claims(21), passages=_PASSAGES, question=_QUESTION)

    assert isinstance(out, tg.GateFailure)
    assert out.reason == "malformed_structure"


def test_the_cap_the_model_is_told_matches_the_cap_enforced() -> None:
    """The advertised schema and the validator must never drift.

    TURN_SCHEMA_MESSAGE is the LAST thing the model reads, so its maxItems is a
    stronger anchor on answer length than any prose instruction. A stale number
    here quietly teaches the model the old limit.
    """
    from regwatch.generate.turn_schema import TURN_SCHEMA_MESSAGE, GroundedTurn

    cap = GroundedTurn.model_fields["claims"].metadata[0].max_length
    assert cap == 20
    assert f'"maxItems":{cap}' in TURN_SCHEMA_MESSAGE.content


def test_an_unsupported_label_is_bounded_to_a_short_label() -> None:
    """The list was capped at 2; the ITEMS were unbounded.

    The prompt asks for "SHORT LABELS" and nothing enforced it, so two
    2,000-char strings could add ~2,000 output tokens to a payload the budget
    sized at roughly 30.
    """
    ok = tg.admit_turn(
        synth_turn_json([], turn_type="ANSWER", unsupported=("x" * 80,)),
        passages=_PASSAGES,
        question=_QUESTION,
    )
    assert isinstance(ok, tg.AdmittedTurn)

    too_long = tg.admit_turn(
        synth_turn_json([], turn_type="ANSWER", unsupported=("x" * 81,)),
        passages=_PASSAGES,
        question=_QUESTION,
    )
    assert isinstance(too_long, tg.GateFailure)


# ---------- the citation corrector and the epistemic ledger fields ----------
#
# DARK in v5: no production caller passes correct=True, so today these paths
# surface only as ledger fields at their defaults. The tests pin the mechanics
# now so flipping the flag later is a config change, not a behavior gamble.

_UNIFORM_META = {"dosage_form": "Aerosol, Metered", "route": "Inhalation"}

# Exactly 5 scoreable tokens -- fasting, crossover, design, healthy, subjects
# ("in" is under the length floor) -- so passage overlaps land on exact fifths
# and the floor/margin boundaries are testable without float surprises.
_CORRECTABLE_CLAIM = "Fasting crossover design in healthy subjects"
_BEST_TEXT = "Fasting crossover design procedures for healthy adult volunteers."  # 4/5
_OFF_TOPIC_TEXT = "Dissolution testing with the paddle apparatus."  # 0/5


def _uniform_passage(page: int, chunk_id: str, text: str) -> RetrievedPassage:
    return _passage(page=page, chunk_id=chunk_id, text=text, metadata=dict(_UNIFORM_META))


def test_correct_true_restamps_an_unknown_cite_onto_the_unambiguous_passage() -> None:
    passages = [
        _uniform_passage(3, "chunk-best", _BEST_TEXT),
        _uniform_passage(4, "chunk-other", _OFF_TOPIC_TEXT),
    ]
    turn = _admit(
        synth_turn_json([(_CORRECTABLE_CLAIM, [("PSG_999999", 7)])]), passages, correct=True
    )

    assert turn.verdict == tg.VERDICT_ANSWER
    assert not turn.dropped
    claim = turn.admitted[0]
    assert claim.pairs == (("PSG_020503", 3),)
    assert claim.correction_method == tg.CORRECTION_LEXICAL
    assert claim.original_cites == (("PSG_999999", 7),)
    assert claim.kind == tg.CLAIM_KIND_SOURCE_FACT
    assert claim.downgraded is False
    # The citation binds the chunk whose TEXT won the argmax.
    assert tg.citations(turn)[0].chunk_id == "chunk-best"


def test_correction_accepts_at_the_exact_floor() -> None:
    at_floor = _passage(text="Fasting crossover design procedures.")  # 3/5 == the floor

    out = tg.correct_unknown_citation(_CORRECTABLE_CLAIM, (("PSG_999999", 7),), [at_floor])

    assert out is at_floor


def test_correction_rejects_below_the_floor() -> None:
    weak = _passage(text="Fasting procedures overview.")  # 1/5

    assert tg.correct_unknown_citation(_CORRECTABLE_CLAIM, (("PSG_999999", 7),), [weak]) is None


def test_correction_accepts_at_the_exact_margin_over_the_runner_up() -> None:
    best = _passage(chunk_id="chunk-best", text=_BEST_TEXT)  # 4/5
    runner = _passage(page=4, chunk_id="chunk-runner", text="Fasting crossover design overview.")

    # Runner-up listed first: the winner is found by score, not list position.
    out = tg.correct_unknown_citation(_CORRECTABLE_CLAIM, (), [runner, best])

    assert out is best


def test_correction_rejects_a_near_tie_inside_the_margin() -> None:
    """A near-tie means the evidence cannot say WHICH passage the model meant,
    and a guessed re-stamp is the OD-4 bug the gate exists to prevent."""
    best = _passage(chunk_id="chunk-best", text=_BEST_TEXT)  # 4/5
    tie = _passage(page=4, chunk_id="chunk-tie", text="Healthy fasting crossover design summary.")

    assert tg.correct_unknown_citation(_CORRECTABLE_CLAIM, (), [best, tie]) is None


def test_correction_is_negation_blind_so_material_claims_return_none() -> None:
    """The P0 (F1): "not required" overlaps the passage saying it IS required,
    so a material claim must never reach the argmax at all."""
    claim = "A fed study is not required for the 45 mcg strength"
    inverting = _passage(text="A fed study is required for the 45 mcg strength in adults.")

    assert tg.correct_unknown_citation(claim, (("PSG_999999", 7),), [inverting]) is None


def test_material_claim_is_never_corrected_and_lands_on_material_drop() -> None:
    """F1 end to end: overwhelming overlap with the INVERTING passage, every
    other precondition satisfied -- the claim still drops, the turn is still
    rejected as material, and the exemption is ledgered so its rate shows up."""
    passages = [
        _uniform_passage(3, "chunk-best", _BEST_TEXT),
        _uniform_passage(5, "chunk-fed", "A fed study is required for the 45 mcg strength."),
    ]
    turn = _admit(
        synth_turn_json(
            [
                (_CORRECTABLE_CLAIM, [("PSG_020503", 3)]),
                ("A fed study is not required for the 45 mcg strength", [("PSG_999999", 7)]),
            ]
        ),
        passages,
        correct=True,
    )

    assert turn.verdict == tg.VERDICT_MATERIAL_DROP
    assert turn.material_word == "not"
    assert [c.reason for c in turn.dropped] == [tg.DROP_UNKNOWN_CITATION]
    assert turn.dropped[0].correction_method == tg.CORRECTION_MATERIAL_EXEMPT
    assert turn.admitted[0].correction_method is None  # the valid sibling is untouched
    led = tg.ledger(turn, model="stub-model", prompt_version="6")
    row = next(c for c in led["claims"] if not c["admitted"])
    assert row["correction_method"] == tg.CORRECTION_MATERIAL_EXEMPT


def test_material_uncited_claim_is_never_downgraded() -> None:
    turn = _admit(
        synth_turn_json(
            [
                (_CORRECTABLE_CLAIM, [("PSG_020503", 3)]),
                ("A fed study is not required for the 45 mcg strength", []),
            ]
        ),
        [_uniform_passage(3, "chunk-best", _BEST_TEXT)],
        correct=True,
    )

    assert turn.verdict == tg.VERDICT_MATERIAL_DROP
    assert [c.reason for c in turn.dropped] == [tg.DROP_NO_CITES]
    assert turn.dropped[0].correction_method == tg.CORRECTION_MATERIAL_EXEMPT
    assert all(not c.downgraded for c in turn.admitted)


@pytest.mark.parametrize(
    "passages",
    [
        # Two dosage forms: the single product/form premise fails outright.
        [
            _uniform_passage(3, "chunk-a", _BEST_TEXT),
            _passage(
                page=4,
                chunk_id="chunk-b",
                text=_OFF_TOPIC_TEXT,
                metadata={"dosage_form": "Tablet", "route": "Oral"},
            ),
        ],
        # Empty metadata: ingest wrote nothing, so the upstream mixed-form
        # guards skipped this passage and uniformity cannot be proven.
        [_passage(chunk_id="chunk-a", text=_BEST_TEXT)],
        # Empty product name, same argument.
        [
            _passage(
                chunk_id="chunk-a",
                text=_BEST_TEXT,
                normalized_name="",
                metadata=dict(_UNIFORM_META),
            )
        ],
    ],
)
def test_nonuniform_or_empty_metadata_falls_back_to_todays_drop(
    passages: list[RetrievedPassage],
) -> None:
    """F6: correction's premise is metadata-conditional, so an unprovable
    premise means today's drop behavior, never a best-effort re-stamp."""
    turn = _admit(
        synth_turn_json([(_CORRECTABLE_CLAIM, [("PSG_999999", 7)])]), passages, correct=True
    )

    assert turn.verdict == tg.VERDICT_NO_VALID_CITATIONS
    assert [c.reason for c in turn.dropped] == [tg.DROP_UNKNOWN_CITATION]
    assert turn.dropped[0].correction_method is None


def test_downgrade_prepends_the_gate_frame_and_clears_cites() -> None:
    claim = tg.AdmittedClaim(
        index=2,
        text="The two documents describe the same crossover design",
        pairs=(("PSG_020503", 3),),
        citations=(),
        overlap=0.5,
    )

    down = tg.downgrade_to_reasoning(claim)

    assert down.text == f"{tg.REASONING_FRAME}The two documents describe the same crossover design"
    assert down.kind == tg.CLAIM_KIND_REASONING
    assert down.downgraded is True
    assert down.pairs == ()
    assert down.citations == ()
    assert down.index == 2


def test_downgrade_is_unreachable_for_a_material_claim() -> None:
    claim = tg.AdmittedClaim(
        index=0, text="A fed study is not required", pairs=(), citations=(), overlap=0.0
    )

    with pytest.raises(ValueError, match="material"):
        tg.downgrade_to_reasoning(claim)


def test_uncited_benign_claim_downgrades_to_reasoning_when_correct_is_on() -> None:
    turn = _admit(
        synth_turn_json([("The two guidances describe the same crossover design", [])]),
        correct=True,
    )

    assert turn.verdict == tg.VERDICT_ANSWER
    assert not turn.dropped
    claim = turn.admitted[0]
    assert claim.kind == tg.CLAIM_KIND_REASONING
    assert claim.downgraded is True
    assert claim.pairs == ()
    assert claim.citations == ()
    assert claim.text.startswith(tg.REASONING_FRAME)
    led = tg.ledger(turn, model="stub-model", prompt_version="6")
    row = led["claims"][0]
    assert row["kind"] == tg.CLAIM_KIND_REASONING
    assert row["downgraded"] is True
    assert row["cites"] == []


def test_correct_defaults_off_and_admission_is_unchanged() -> None:
    """The dark-launch contract: without correct=True the gate behaves exactly
    as today even when every correction precondition holds, and the new fields
    ride the ledger at their defaults."""
    passages = [
        _uniform_passage(3, "chunk-best", _BEST_TEXT),
        _uniform_passage(4, "chunk-other", _OFF_TOPIC_TEXT),
    ]
    payload = synth_turn_json(
        [
            (_CORRECTABLE_CLAIM, [("PSG_020503", 3)]),
            ("The reference product is a metered aerosol", []),
            ("The crossover design uses healthy adult volunteers", [("PSG_999999", 7)]),
        ]
    )

    out = tg.admit_turn(payload, passages=passages, question=_QUESTION)

    assert isinstance(out, tg.AdmittedTurn)
    assert out.verdict == tg.VERDICT_PARTIAL
    assert [c.reason for c in out.dropped] == [tg.DROP_NO_CITES, tg.DROP_UNKNOWN_CITATION]
    assert all(c.correction_method is None for c in out.dropped)
    kept = out.admitted[0]
    assert (kept.kind, kept.correction_method, kept.original_cites, kept.downgraded) == (
        tg.CLAIM_KIND_SOURCE_FACT,
        None,
        None,
        False,
    )
    led = tg.ledger(out, model="stub-model", prompt_version="5")
    for row in led["claims"]:
        assert row["kind"] == tg.CLAIM_KIND_SOURCE_FACT
        assert row["correction_method"] is None
        assert row["original_cites"] is None
        assert row["downgraded"] is False
    assert json.loads(json.dumps(led))["claims"][0]["downgraded"] is False


def test_ledger_serializes_the_correction_fields() -> None:
    passages = [
        _uniform_passage(3, "chunk-best", _BEST_TEXT),
        _uniform_passage(4, "chunk-other", _OFF_TOPIC_TEXT),
    ]
    turn = _admit(
        synth_turn_json([(_CORRECTABLE_CLAIM, [("PSG_999999", 7)])]), passages, correct=True
    )

    led = tg.ledger(turn, model="stub-model", prompt_version="6")

    # json round-trip first: the ledger is persisted verbatim inside route_json.
    row = json.loads(json.dumps(led))["claims"][0]
    assert row["admitted"] is True
    assert row["kind"] == tg.CLAIM_KIND_SOURCE_FACT
    assert row["correction_method"] == tg.CORRECTION_LEXICAL
    assert row["original_cites"] == ["PSG_999999,p.7"]
    assert row["cites"] == ["PSG_020503,p.3"]
    assert row["downgraded"] is False
