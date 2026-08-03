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
) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=chunk_id,
        text=text,
        score=score,
        doc_id=1,
        version_id=10,
        page=page,
        section_path=None,
        normalized_name="albuterol sulfate",
        source_url=f"http://example/{short_name}.pdf",
        short_name=short_name,
        metadata={},
    )


_PASSAGES = [_passage(), _passage(page=4, chunk_id="chunk-2", text="Dissolution: USP paddle.")]
_QUESTION = "What study design and dissolution method are recommended?"


def _admit(raw: str, passages: list[RetrievedPassage] | None = None) -> tg.AdmittedTurn:
    out = tg.admit_turn(
        raw, passages=_PASSAGES if passages is None else passages, question=_QUESTION
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
