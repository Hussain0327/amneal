"""v7 selective citation (REGWATCH_SELECTIVE_CITATION), on top of v6 prose.

Shape mirrors tests/test_prose_synthesis.py: identity/wiring pins first, then
end-to-end behavior through ask() as later stages land the gate/renderer/
grounded_qa changes B.10 describes. Test IDs (T-1..T-15) match the plan in
docs/V7_DESIGN_PARKED_2026-08-10.md B.10.6; this file grows stage by stage
(S1..S5) exactly as the code they exercise lands -- a test id's FULL shape is
only reachable once its dependencies exist, so earlier stages carry the
S-stage-appropriate subset of each assertion.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import prompts
from regwatch.generate import prose_turn as pt
from regwatch.generate import turn_gate as tg
from regwatch.generate.llm import LLMResponse
from regwatch.retrieve.retriever import RetrievedPassage
from tests.test_invariants import _meta, _only_route_json, _seed_corpus

pytestmark = pytest.mark.invariants

_QUESTION = "What study design does the albuterol sulfate PSG recommend?"
_CORPUS = [("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))]


def _v7_mode(monkeypatch: pytest.MonkeyPatch, *, prose: bool = True) -> None:
    """Flag the turn into v7 selective citation.

    ``prose=False`` reaches the selective-without-prose (risk 6) topology: the
    flag is set but inert because prose_synthesis_enabled is not. Drops the
    retrieval floor to reach synthesis, mirroring test_prose_synthesis._prose_mode.
    """
    if prose:
        monkeypatch.setenv("REGWATCH_PROSE_SYNTHESIS", "1")
    monkeypatch.setenv("REGWATCH_SELECTIVE_CITATION", "1")
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()


def _stub_llm(text: str) -> Any:
    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    return _LLM()


# ---------- T-1: identity pins ----------


def test_v7_identity_pins() -> None:
    assert prompts.GROUNDED_QA_SYSTEM_V7.splitlines()[0] == "[REGWATCH_GROUNDED_QA_V7]"
    assert prompts.GROUNDED_QA_PROMPT_V7.version == "7"
    assert prompts.GROUNDED_QA_PROMPT_V7.sha256 != prompts.GROUNDED_QA_PROMPT.sha256
    assert prompts.GROUNDED_QA_PROMPT_V7.sha256 != prompts.GROUNDED_QA_PROMPT_V6.sha256
    # Zero-shot by decision (2026-08-20). Re-adding a pair is deliberate and
    # must come with a gold-set result, so pin the empty tuple.
    assert prompts.GROUNDED_QA_EXEMPLARS_V7 == ()


# ---------- T-2: no sentinel anywhere ----------


def test_v7_prompt_carries_no_no_evidence_sentinel() -> None:
    """v7 has no code word for found-nothing (B.10.1.8): NO_EVIDENCE must be
    absent from every v7-served string, system and user templates alike."""
    texts = [
        prompts.GROUNDED_QA_SYSTEM_V7,
        prompts.GROUNDED_QA_USER_V7,
        prompts.GROUNDED_QA_V7_EXEMPLAR_ANSWER_USER,
        prompts.GROUNDED_QA_V7_EXEMPLAR_ANSWER_ASSISTANT,
        prompts.GROUNDED_QA_V7_EXEMPLAR_CLARIFY_USER,
        prompts.GROUNDED_QA_V7_EXEMPLAR_CLARIFY_ASSISTANT,
        prompts.GROUNDED_QA_V7_EXEMPLAR_NO_EVIDENCE_USER,
        prompts.GROUNDED_QA_V7_EXEMPLAR_NO_EVIDENCE_ASSISTANT,
    ]
    for text in texts:
        assert "NO_EVIDENCE" not in text


# ---------- T-3: frame byte-pin (S1 half: prose_turn's current location;
# the tg-identity half is added in S2 once REASONING_FRAME_PREFIXES moves) ----------


def test_v7_gate_authored_frame_is_recognized_by_its_own_parser() -> None:
    """The prompt no longer teaches the frame openers (2026-08-20).

    Under v7 an unframed uncited sentence classifies as CONVERSATION and is
    ADMITTED -- ``_classify_uncited_selective`` only returns ``source_fact``
    when materiality or source-assertion fires, so a frame was never required
    for admission. It only relabels conversation -> reasoning.

    What still MUST hold is narrower and real: turn_gate authors
    ``REASONING_FRAME`` itself when it corrects a claim, so that frame has to
    be recognized by the same parser, or the gate's own output would be
    reclassified as an uncited source fact and dropped.
    """
    assert tg.REASONING_FRAME.lower().startswith(tg.REASONING_FRAME_PREFIXES)
    # Provenance is preserved without the ritual: an uncited sentence that
    # asserts what a source says is still a source_fact, framed or not.
    assert pt._classify_uncited_selective("FDA requires a fasting study") == "source_fact"
    assert pt._classify_uncited_selective("I would check the BE guidance next") == "conversation"


def test_frame_prefixes_moved_to_turn_gate_and_prose_turn_reexports_it() -> None:
    """B.10.3.2: the frame vocabulary lives in turn_gate now (both the
    selective classifier AND render_decline's guard need it, and a
    turn_gate -> prose_turn import would cycle). prose_turn keeps the same
    object under its old name so every existing reference still resolves."""
    from regwatch.generate import turn_gate as tg

    assert pt.REASONING_FRAME_PREFIXES is tg.REASONING_FRAME_PREFIXES


# ---------- T-4: exemplars survive their own gate ----------


def _dummy_passages(n: int) -> list[RetrievedPassage]:
    return [
        RetrievedPassage(
            chunk_id=f"chunk-{i}",
            text="x",
            score=1.0,
            doc_id=i,
            version_id=i,
            page=i,
            section_path=None,
            normalized_name="exemplostat",
            source_url="http://example/x.pdf",
            short_name=f"PSG_EXAMPLE{i}",
            metadata={},
        )
        for i in range(1, n + 1)
    ]


def test_v7_exemplars_survive_their_own_gate() -> None:
    """T-4: every uncited sentence in the three assistant exemplars must pass
    BOTH lexicons the gate runs, and _classify_uncited_selective must give
    exactly the kinds B.10.2's table says -- an exemplar the gate would drop
    teaches the model a shape that never renders."""
    from regwatch.generate.turn_gate import (
        frame_split,
        materiality_trigger,
        source_assertion_trigger,
    )

    cases = [
        (
            prompts.GROUNDED_QA_V7_EXEMPLAR_ANSWER_ASSISTANT,
            2,
            ["source_fact", "source_fact", "reasoning", "conversation"],
        ),
        (
            prompts.GROUNDED_QA_V7_EXEMPLAR_CLARIFY_ASSISTANT,
            2,
            ["source_fact", "source_fact", "conversation"],
        ),
        (
            prompts.GROUNDED_QA_V7_EXEMPLAR_NO_EVIDENCE_ASSISTANT,
            0,
            ["conversation", "conversation", "conversation"],
        ),
    ]
    for text, n_passages, expected_kinds in cases:
        parsed = pt.parse(text, passages=_dummy_passages(n_passages), selective=True)
        assert [c.kind for c in parsed.claims] == expected_kinds
        assert parsed.leftover_brackets == []
        for claim in parsed.claims:
            if claim.cite_indices:
                continue
            scan = frame_split(claim.text)[1] or claim.text
            assert materiality_trigger(scan) is None
            assert source_assertion_trigger(scan) is None


# ---------- T-5 (S1 half): selective-without-prose serves the v5 prompt.
# The warning + admission-unchanged half lands in S4 with the grounded_qa wiring. ----------


def test_selective_without_prose_serves_v5_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    _v7_mode(monkeypatch, prose=False)
    assert prompts.active_grounded_qa_prompt() is prompts.GROUNDED_QA_PROMPT


def test_selective_with_prose_serves_v7_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    _v7_mode(monkeypatch, prose=True)
    assert prompts.active_grounded_qa_prompt() is prompts.GROUNDED_QA_PROMPT_V7


def test_prose_alone_still_serves_v6_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag-off byte-stability half: v6 with selective unset/false is untouched."""
    monkeypatch.setenv("REGWATCH_PROSE_SYNTHESIS", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    assert prompts.active_grounded_qa_prompt() is prompts.GROUNDED_QA_PROMPT_V6


def test_selective_without_prose_warns_and_admission_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-5, S4 half: risk 6 end to end. The turn runs the v5 path exactly
    (proven by the canonical v5 echo shape) and the misconfiguration is
    observable, not silent."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=False)
    warnings: list[str] = []
    monkeypatch.setattr(qa_mod.log, "warning", lambda event, **kw: warnings.append(event))

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert result.status == "answer"
    assert "ECHO grounded test answer [PSG_020503, p.3]." in result.answer
    assert "selective_citation_without_prose" in warnings


# ---------- T-6: echo answer end-to-end ----------


def test_v7_echo_answer_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert result.status == "answer"
    assert "ECHO grounded test answer [PSG_020503, p.3]." in result.answer
    assert "Let me know if you want the dissolution details as well." in result.answer
    assert "[1]" not in result.answer
    assert "\n\nSources:\n" in result.answer
    route = _only_route_json()
    assert route["prompt"]["version"] == "7"
    turn = route["turn"]
    assert turn["renderer_version"] == tg.RENDERER_VERSION_SELECTIVE
    assert turn["claims"][1]["kind"] == "conversation"
    assert turn["claims"][1]["cites"] == []
    assert turn["kind_counts"] == {"source_fact": 1, "conversation": 1}


# ---------- T-7: served conversational decline ----------


def test_v7_served_conversational_decline(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setenv("REGWATCH_ECHO_FORCE_REFUSAL", "1")

    result = qa_mod.ask(_QUESTION)

    assert result.status == "refused"
    assert result.reason == "model_refusal"
    assert result.refused is True
    assert result.citations == []
    assert result.answer == (
        "ECHO has nothing on that question in these passages. "
        "Want me to try a different phrasing?"
    )
    assert "Sources:" not in result.answer
    from config.settings import get_settings

    assert result.answer != get_settings().refusal_text
    turn = _only_route_json()["turn"]
    assert turn["verdict"] == "conversational_decline"
    assert turn["renderer_version"] == tg.RENDERER_VERSION_SELECTIVE
    assert "decline_guard" not in turn


# ---------- T-8/T-9: natural-path declines fall back to canned copy ----------


def test_v7_ais_decline_uses_canned_copy_natural_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-8: the parser reclassifies the uncited attribution sentence to
    source_fact (P1/AIS guard) before the gate ever runs, so it drops on
    no_cites -- never a guard fire, exactly as B.10.1.6 states."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm("FDA recommends a fed study for the 45 mcg strength."),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "no_valid_citations"
    from config.settings import get_settings

    assert result.answer == get_settings().refusal_text
    assert "fed study" not in result.answer
    dropped = [c for c in _only_route_json()["turn"]["claims"] if not c["admitted"]]
    assert [c["drop_reason"] for c in dropped] == ["no_cites"]


def test_v7_material_decline_uses_canned_copy_natural_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-9: same shape as T-8, via the materiality lexicon instead of AIS."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm("A fed study is not required for this product."),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "no_valid_citations"
    from config.settings import get_settings

    assert result.answer == get_settings().refusal_text
    dropped = [c for c in _only_route_json()["turn"]["claims"] if not c["admitted"]]
    assert [c["drop_reason"] for c in dropped] == ["no_cites"]
    assert [c["material_word"] for c in dropped] == ["not"]
    assert [c["correction_method"] for c in dropped] == ["material_exempt"]


# ---------- T-10: guard fires, end-to-end twin ----------


def test_v7_decline_guard_fallback_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch the classifier itself so the parser and the gate disagree
    (as if a caller passed mismatched kinds) -- proving render_decline's
    defense in depth actually reaches the wire: canned copy is served and the
    ledger records the guard."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm("A fed study is not required for this product."),
    )
    monkeypatch.setattr(pt, "_classify_uncited_selective", lambda text, block=None: "conversation")

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "model_refusal"
    from config.settings import get_settings

    assert result.answer == get_settings().refusal_text
    assert "fed study" not in result.answer
    turn = _only_route_json()["turn"]
    assert turn["verdict"] == "conversational_decline"
    assert turn["decline_guard"] == tg.DECLINE_GUARD_MATERIAL


# ---------- T-11: boundary, end-to-end twin ----------


def test_v7_one_cited_sentence_plus_conversation_is_a_normal_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn with >= 1 admitted source fact renders as a normal answer, never
    a decline, even with an uncited conversational sentence alongside it."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "Fasting study with subjects [1]. Happy to dig into the details together."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert result.status == "answer"
    assert "Fasting study with subjects [PSG_020503, p.3]." in result.answer
    assert "Happy to dig into the details together." in result.answer
    assert tg.PARTIAL_DROP_DISCLOSURE not in result.answer
    assert _only_route_json()["turn"]["verdict"] == "answer"


# ---------- T-12: framed benign served, end-to-end twin ----------


def test_v7_framed_benign_reasoning_served_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "Fasting study with subjects [1]. My reading is that the two designs match."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert "My reading is that the two designs match." in result.answer
    assert tg.REASONING_FRAME not in result.answer
    claims = _only_route_json()["turn"]["claims"]
    assert claims[1]["kind"] == "reasoning"
    assert claims[1]["cites"] == []


# ---------- T-13: framed material caught, end-to-end twin ----------


def test_v7_framed_material_sentence_rejects_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "Fasting study with subjects [1]. The guidance does not state this "
            "directly; my reading is that a fed study is not required."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "material_drop"
    assert result.answer == tg.MATERIAL_DROP_TEXT
    dropped = [c for c in _only_route_json()["turn"]["claims"] if not c["admitted"]]
    assert [c["correction_method"] for c in dropped] == ["material_exempt"]
    assert [c["material_word"] for c in dropped] == ["not"]


# ---------- T-14: markup sanitize-keep, end-to-end twin ----------


def test_v7_heading_sanitize_keep_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanitize-keep applies to UNCITED claims -- the case B.10.3.4 justified
    the strip for. A CITED heading-prefixed claim is a separate case (see the
    corrector-widening regression test below): post-launch-review fix P2
    guards the strip to `selective and not declared`."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "Fasting study with subjects [1]. # Happy to dig into the details together."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert "Fasting study with subjects [PSG_020503, p.3]." in result.answer
    assert "Happy to dig into the details together." in result.answer
    assert "#" not in result.answer


def test_v7_heading_prefixed_cited_claim_drops_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-launch-review regression (P2), end to end: a heading-prefixed
    claim that DECLARES a cite (here, a fabricated one -- passage [9] does
    not exist) must not be sanitize-kept and fed to the lexical corrector. It
    stays on DROP_MARKUP, exactly as v5/v6 would refuse it."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("# Study design [9].")
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "no_valid_citations"


def test_v7_link_markup_still_drops_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm("See https://example.com for details [1]."),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "no_valid_citations"


# ---------- post-launch-review regression (P0, FIX-1): expanded SOURCE_ASSERTION_WORDS ----------
# The original 18-word list missed ordinary obligation/attribution phrasing a
# real model plausibly writes. These are the adversarial INV-1 lens's own
# probed evidence strings (decline + answer surfaces combined, deduped).
# "converts 10 of the 15 probed leaks to source_fact" -- verified here as 12
# of 15 with the actually-implemented (existing-list-union) lexicon; the
# residual 3 are pinned separately as the documented Checkpoint-2 open item.

_EXPANDED_LEXICON_CAUGHT = [
    "The applicant should conduct a single-dose fasting study for this product.",
    "There is no dissolution requirement for this dosage form.",
    "The guidance would require a fed study for the 45 mcg strength.",
    "In vivo testing is waived for the lower strengths.",
    "The guidance says a single-dose fasting study is enough for this product.",
    "FDA notes that Q1/Q2 sameness supports the in vitro route.",
    "The PSG calls for a comparative clinical endpoint study.",
    "Beyond the guidance, the applicant should run a fed study as well.",
    "The applicant should also run a fed study for the 45 mcg strength.",
    "There is no dissolution requirement for the capsule form.",
    "In vivo testing is waived for the two lower strengths.",
    "The guidance says the sample size is 24 healthy adult volunteers.",
]

# The 3 residual bald-fact/prepositional sentences (no obligation word, no
# attribution verb) stay open by design (B.10.3.1) -- also listed verbatim in
# docs/DECISIONS.md as the Checkpoint-2 human-review item.
_EXPANDED_LEXICON_RESIDUAL = [
    "Per the 2019 revision, subjects are dosed under fasting conditions.",
    "Under the guidance, the sample size is 24 healthy volunteers.",
    "The dissolution method is Apparatus II at 50 rpm.",
]


@pytest.mark.parametrize("sentence", _EXPANDED_LEXICON_CAUGHT)
def test_expanded_lexicon_leak_drops_on_the_natural_path(sentence: str) -> None:
    """Classification surface: the REAL parser (prose_turn.parse, selective
    classifier) must now read each probed leak as source_fact, and the REAL
    gate must then drop it uncited -- the natural path, no lied kinds."""
    parsed = pt.parse(sentence, passages=[], selective=True)
    assert [c.kind for c in parsed.claims] == ["source_fact"]

    turn = tg.admit_claims(
        parsed.turn_type,
        pt.to_claims(parsed, []),
        passages=[],
        question=_QUESTION,
        correct=True,
        downgrade_uncited=False,
        kinds=[c.kind for c in parsed.claims],
        selective=True,
    )
    assert isinstance(turn, tg.AdmittedTurn)
    assert turn.admitted == ()
    assert [d.reason for d in turn.dropped] == [tg.DROP_NO_CITES]
    assert turn.verdict == tg.VERDICT_NO_VALID_CITATIONS


@pytest.mark.parametrize("sentence", _EXPANDED_LEXICON_RESIDUAL)
def test_residual_bald_fact_sentences_stay_uncaught_by_design(sentence: str) -> None:
    """Pins the documented Checkpoint-2 open item: these 3 sentences carry
    neither an obligation word nor an attribution verb, so the classifier
    reads them as conversation and the gate admits them uncited. Not a
    regression -- a boundary pin so nobody "fixes" this by guesswork (an
    overlap-threshold guard is explicitly deferred, B.10.3.1)."""
    parsed = pt.parse(sentence, passages=[], selective=True)
    assert [c.kind for c in parsed.claims] == ["conversation"]


def test_v7_fix2_dropped_source_fact_plus_filler_refuses_with_canned_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX-2 (P1) end to end, decline surface. The reviewer's exact stub: a
    dropped SOURCE FACT (no cites) alongside harmless uncited filler must NOT
    render as a conversational 'Evidence gap' serving the orphaned filler --
    it refuses with the canned copy, the same outcome v6 reaches for the
    identical completion (there the filler would drop too)."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "FDA recommends a fed study for the 45 mcg strength. "
            "Let me know if you want the dissolution details as well."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "no_valid_citations"
    from config.settings import get_settings

    assert result.answer == get_settings().refusal_text
    assert "Let me know" not in result.answer
    assert "fed study" not in result.answer
    turn = _only_route_json()["turn"]
    assert turn["verdict"] == "no_valid_citations"


def test_v7_fix2_dangling_referent_after_unknown_citation_refuses_with_canned_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX-2, the dangling-referent case: an unresolvable marker drops its
    sentence (unknown_citation, the corrector's overlap floor is not cleared
    on this stub), leaving a filler sentence whose referent was deleted
    ("That is the same design..."). Must refuse with canned copy, not serve
    the orphan."""
    _seed_corpus(_CORPUS)
    _v7_mode(monkeypatch, prose=True)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "FDA recommends a single-dose fasting study [9]. "
            "That is the same design used for the 25 mg strength."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "no_valid_citations"
    from config.settings import get_settings

    assert result.answer == get_settings().refusal_text
    assert "25 mg strength" not in result.answer
    turn = _only_route_json()["turn"]
    assert turn["verdict"] == "no_valid_citations"
