"""v6 prose synthesis (REGWATCH_PROSE_SYNTHESIS) end-to-end through ask().

Same refuse-or-cite POLICY as v5, prose + [n] FORMAT: these tests drive the
full flag-on chain -- v6 prompt build, prose parse, the claims bridge, gate
admission with re-stamp correction live and the uncited-downgrade path OFF --
over the real retrieval stack, with the echo provider or a fixed stub.

The flag-off suite (everything else in tests/) is the byte-identity proof for
v5; nothing here runs with the flag off except the identity pins.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import prompts
from regwatch.generate import turn_gate as tg
from regwatch.generate.llm import LLMResponse
from regwatch.generate.prose_turn import PROSE_NO_EVIDENCE_SENTINEL
from tests.test_invariants import _meta, _only_route_json, _seed_corpus

pytestmark = pytest.mark.invariants

_QUESTION = "What study design does the albuterol sulfate PSG recommend?"
# One metadata-complete passage: normalized_name/dosage_form/route are all
# truthy and uniform, so the gate's correction precondition (F6) holds.
_CORPUS = [("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))]


def _prose_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag the turn into v6 prose; drop the retrieval floor to reach synthesis."""
    monkeypatch.setenv("REGWATCH_PROSE_SYNTHESIS", "1")
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()


def _stub_llm(text: str) -> Any:
    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    return _LLM()


# ---------- identity pins ----------


def test_v6_identity_and_sentinel_pins() -> None:
    """The wiring constants three modules key on, pinned in one place.

    The echo provider and the synthesis branch key on the system sentinel; the
    parser keys on the NO_EVIDENCE sentinel the prompt instructs; the identity
    is version 6 with a hash of its own (exemplars in, schema message out).
    """
    assert prompts.GROUNDED_QA_SYSTEM_V6.splitlines()[0] == "[REGWATCH_GROUNDED_QA_V6]"
    # The instruction wording must match the parser's sentinel EXACTLY -- the
    # prompt cannot import prose_turn (import-graph flatness), so this test is
    # the coupling.
    assert f"exactly: {PROSE_NO_EVIDENCE_SENTINEL}" in prompts.GROUNDED_QA_SYSTEM_V6
    assert PROSE_NO_EVIDENCE_SENTINEL in prompts.GROUNDED_QA_USER_V6
    assert prompts.GROUNDED_QA_PROMPT_V6.version == "6"
    assert prompts.GROUNDED_QA_PROMPT_V6.sha256 != prompts.GROUNDED_QA_PROMPT.sha256
    # Exemplars alternate user/assistant, starting with user, ending assistant.
    roles = [role for role, _ in prompts.GROUNDED_QA_EXEMPLARS_V6]
    assert roles == ["user", "assistant", "user", "assistant"]


# ---------- echo end-to-end ----------


def test_flag_on_echo_serves_cited_prose_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Echo emits "ECHO grounded test answer [1]."; the rendered wire is v5-shaped.

    The model-facing [n] marker must NEVER leak: the renderer writes canonical
    [SHORT_NAME, p.N] markers plus the Sources trailer, so the frontend
    contract is untouched by the format flip.
    """
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert result.status == "answer"
    assert "ECHO grounded test answer [PSG_020503, p.3]." in result.answer
    assert "[1]" not in result.answer
    assert "\n\nSources:\n" in result.answer
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020503", 3)}
    route = _only_route_json()
    assert route["prompt"]["version"] == "6"
    turn = route["turn"]
    assert turn["prompt_version"] == "6"
    assert turn["verdict"] == "answer"
    assert turn["claims"][0]["kind"] == "source_fact"
    assert route["synthesis"]["prose_parse"] == {
        "claims_parsed": 1,
        "killed": [],
        "truncated_material": False,
    }


def test_flag_on_echo_forced_refusal_is_model_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prose NO_EVIDENCE sentinel lands on the same model_refusal branch as v5."""
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setenv("REGWATCH_ECHO_FORCE_REFUSAL", "1")

    result = qa_mod.ask(_QUESTION)

    assert result.reason == "model_refusal"
    assert result.citations == []
    assert "ECHO" not in result.answer


def test_flag_on_echo_forced_malformed_is_no_sentences_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unterminated garbage -> zero parsed sentences -> malformed_structure.

    The reason string is kept so ops greps and the prod baseline stay
    comparable; in prose mode it means "no sentences parsed".
    """
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setenv("REGWATCH_ECHO_FORCE_MALFORMED", "1")

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.status == "error"
    assert result.reason == "malformed_structure"
    assert result.citations == []


# ---------- corrector wiring (stubbed synthesizer) ----------


def test_unknown_numeric_marker_is_corrected_onto_matching_passage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-range [n] on a benign, lexically-anchored sentence re-stamps.

    The parser carries the marker as declared-but-unresolvable, the bridge
    turns it into an unknown cite, and the gate's lexical corrector re-stamps
    it onto the one passage it overlaps -- ledgered, not silent.
    """
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("Fasting study with subjects [7].")
    )

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert "Fasting study with subjects [PSG_020503, p.3]." in result.answer
    claim_row = _only_route_json()["turn"]["claims"][0]
    assert claim_row["correction_method"] == "lexical_overlap"
    assert claim_row["original_cites"] == ["UNRESOLVED_7,p.1"]


def test_material_claim_with_unknown_marker_is_never_corrected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 (P0): token overlap is negation-blind, so a material claim stays dropped.

    "not required" would score highly against the passage saying a study IS
    part of the design; the exemption keeps it on the drop path and, with a
    cited neighbour admitted, the material drop rejects the WHOLE answer.
    """
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "Fasting study with subjects [1]. A fed study is not required for this product [7]."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "material_drop"
    assert result.answer == tg.MATERIAL_DROP_TEXT
    dropped = [c for c in _only_route_json()["turn"]["claims"] if not c["admitted"]]
    assert [c["correction_method"] for c in dropped] == ["material_exempt"]
    assert [c["material_word"] for c in dropped] == ["not"]


def test_lone_material_claim_refuses_as_no_valid_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero admitted claims outrank materiality, exactly as in v5.

    When the material claim was the ONLY claim, the turn refuses on
    no_valid_citations (nothing admitted), and the exemption is still
    ledgered so the material_exempt rate is measurable.
    """
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm("A fed study is not required for this product [7]."),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "no_valid_citations"
    claim_row = _only_route_json()["turn"]["claims"][0]
    assert claim_row["admitted"] is False
    assert claim_row["correction_method"] == "material_exempt"


def test_uncited_benign_sentence_is_dropped_not_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v6 policy is still refuse-or-cite: no gate-framed uncited prose is served.

    A benign uncited sentence lands on DROP_NO_CITES exactly as a v5 zero-cite
    claim would; the cited neighbour survives with the partial disclosure. The
    downgrade path stays dark until the v7 selective-citation flip.
    """
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "Fasting study with subjects [1]. Happy to dig into the details together."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert "Happy to dig" not in result.answer
    assert tg.REASONING_FRAME not in result.answer
    assert tg.PARTIAL_DROP_DISCLOSURE in result.answer
    dropped = [c for c in _only_route_json()["turn"]["claims"] if not c["admitted"]]
    assert [c["drop_reason"] for c in dropped] == ["no_cites"]


# ---------- parser-kill safety through the full turn ----------


def test_fabricated_pair_echo_is_killed_with_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-drug pair echo dies with its sentence; the user is told.

    The fabricated [PSG_999999, p.9] names a passage never sent this turn, so
    the parser kills the sentence (a stated source identity that resolves
    nowhere is a fabrication). The kill happens BEFORE the gate, so OD-5
    continuity comes from the verdict fold: the surviving answer discloses the
    omission and the killed text is in the parse ledger.
    """
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "Fasting study with subjects [1]. "
            "The albuterol product uses the same crossover design [PSG_999999, p.9]."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert not result.refused
    assert "PSG_999999" not in result.answer
    assert "crossover" not in result.answer
    assert "Fasting study with subjects [PSG_020503, p.3]." in result.answer
    assert tg.PARTIAL_DROP_DISCLOSURE in result.answer
    route = _only_route_json()
    assert route["turn"]["verdict"] == "partial"
    killed = route["synthesis"]["prose_parse"]["killed"]
    assert len(killed) == 1 and "PSG_999999" in killed[0]


def test_material_parser_kill_rejects_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated materially-worded tail fails the turn, not just the sentence.

    The truncation may have severed a qualifier from the answer that survives,
    so the parse is treated like a material drop one layer early.
    """
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "Fasting study with subjects [1]. The biowaiver is not granted unless"
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert result.refused
    assert result.reason == "material_drop"
    assert result.answer == tg.MATERIAL_DROP_TEXT
    assert "biowaiver" not in result.answer
    route = _only_route_json()
    assert route["synthesis"]["prose_parse"]["truncated_material"] is True
    # The turn died before the gate: no admitted-turn ledger on this row.
    assert "turn" not in route


# ---------- issue #183: legacy claims-JSON caps must not police prose ----------
#
# The prose path used to parse sentences and then re-serialize them into the v5
# claims-JSON contract before the gate saw them (prose_turn.gate_payload ->
# admit_turn -> GroundedTurn.model_validate). That schema's caps were written to
# defend against arbitrary MODEL-authored JSON; on the prose path they policed
# our own sentence splitter, so a perfectly good answer died as
# malformed_structure. Three caps, three cliffs -- issue #183 named only the
# first:
#   turn_schema.Claim.text          max_length=400  -> one long sentence
#   turn_schema.GroundedTurn.claims max_length=20   -> a 21-sentence answer
#   turn_schema.Claim.cites         max_length=4    -> 5 markers on one sentence
# All three were reachable in prod (v6 and v7 are byte-identical here). The
# prose path now reaches turn_gate.admit_claims directly, so none of the caps
# apply to it; they still guard the v5 JSON arm, which still parses a string.

# 460 chars, one sentence, one resolvable marker. Deliberately free of
# obligation words: this must be admitted on its citation, not argued about on
# the materiality path.
_LONG_SENTENCE = (
    "The bioequivalence study design described in this guidance is a single dose "
    "fasting study conducted in healthy adult volunteers with a sample size large "
    "enough to characterize the pharmacokinetic profile of the drug product, and "
    "the discussion also covers the dissolution testing conditions, the analytical "
    "method validation, the statistical analysis plan, the treatment of outlying "
    "results, and the documentation of protocol deviations that accompanies the "
    "submission [1]."
)


def test_sentence_longer_than_the_legacy_json_cap_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#183: a >400-char prose sentence renders instead of failing the turn.

    The sentence is well-formed, cited, and resolvable. Only the v5 JSON
    schema's per-claim text cap stands between it and the user.
    """
    assert len(_LONG_SENTENCE) > 400, "fixture must exceed the legacy 400-char cap"
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_LONG_SENTENCE))

    result = qa_mod.ask(_QUESTION)

    assert not result.refused, f"long sentence was rejected: {result.reason}"
    assert "[PSG_020503, p.3]" in result.answer
    claims = _only_route_json()["turn"]["claims"]
    assert len(claims) == 1
    assert claims[0]["admitted"] is True


def test_answer_with_more_than_twenty_sentences_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#183 sibling: the 20-claim cap kills a long-but-valid prose answer."""
    completion = " ".join(
        f"Study design point number {n} is described in the guidance [1]." for n in range(1, 22)
    )
    _seed_corpus(_CORPUS)
    _prose_mode(monkeypatch)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(completion))

    result = qa_mod.ask(_QUESTION)

    assert not result.refused, f"21-sentence answer was rejected: {result.reason}"
    claims = _only_route_json()["turn"]["claims"]
    assert len(claims) == 21
    assert all(c["admitted"] for c in claims)


def test_sentence_citing_five_passages_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#183 sibling: the 4-cite cap kills a sentence supported by 5 passages.

    All five passages are the same product, form, and document -- only the page
    differs -- so nothing but the cite-count cap can reject this turn.
    """
    _seed_corpus([(f"Fasting BE study detail number {p}.", _meta(1, p)) for p in range(1, 6)])
    _prose_mode(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            "The study design is described across the guidance [1][2][3][4][5]."
        ),
    )

    result = qa_mod.ask(_QUESTION)

    assert not result.refused, f"five-cite sentence was rejected: {result.reason}"
    claims = _only_route_json()["turn"]["claims"]
    assert len(claims) == 1
    assert len(claims[0]["cites"]) == 5


# ---------- marker/passage correspondence guard (pre-refactor characterization) ----------


def _capturing_stub_llm(text: str, sink: list[str]) -> Any:
    """A stub that also records every prompt string it was handed."""

    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            for arg in list(a) + list(kw.values()):
                items = arg if isinstance(arg, (list, tuple)) else [arg]
                for m in items:
                    content = getattr(m, "content", None)
                    if isinstance(content, str):
                        sink.append(content)
            return LLMResponse(text=text, model="stub")

    return _LLM()


def test_marker_resolves_to_the_passage_shown_under_that_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker [n] must stamp the passage the model was SHOWN as [n].

    This is the property the gate cannot check for itself: it verifies that a
    cited (short_name, page) pair EXISTS among this turn's passages, never that
    it is the pair the model meant. An off-by-one or a wrong passage list in
    the prose->gate bridge therefore produces a real, clickable citation on the
    wrong document, with no drop, no warning and no ledger anomaly. Read the
    number the model actually saw out of the prompt rather than assuming a
    retrieval order (the echo embedder ranks by sha256, so the order is
    deterministic but arbitrary).
    """
    seen: list[str] = []
    _seed_corpus(
        [
            ("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503")),
            ("Dissolution testing uses Apparatus II at 50 rpm.", _meta(2, 9, "PSG_040101")),
        ]
    )
    _prose_mode(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _capturing_stub_llm("The design is described in the guidance [2].", seen),
    )

    result = qa_mod.ask(_QUESTION)

    # seen[-1] is THIS turn's user message; earlier entries include the v6
    # few-shot exemplars, whose own numbered blocks would otherwise match.
    shown = re.search(r"\[2\] \[([A-Z0-9_]+), p\.(\d+)\]", seen[-1])
    assert shown is not None, "passage [2] was never shown to the model"
    short, page = shown.group(1), shown.group(2)

    assert not result.refused, f"correspondence turn was rejected: {result.reason}"
    assert f"[{short}, p.{page}]" in result.answer
    cites = _only_route_json()["turn"]["claims"][0]["cites"]
    assert cites == [f"{short},p.{page}"], "marker [2] stamped a passage other than the one shown"


# ---------- pathological-output bounds and the one repair (issue #183) ------
#
# The bounds are a runaway-output guard, NOT a style rule: measured v7 output
# tops out at 488 chars a sentence, so nothing a real answer does comes near
# them. What these pin is the RECOVERY -- one repair attempt, then a
# conversational exit that never shows the user a reason code.


def _v7_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """v6 prose FORMAT plus the v7 selective-citation POLICY."""
    _prose_mode(monkeypatch)
    monkeypatch.setenv("REGWATCH_SELECTIVE_CITATION", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()


def _sequence_stub_llm(texts: list[str], calls: list[str]) -> Any:
    """Return each text in turn, recording every call.

    A list rather than one fixed string, because "exactly one repair" cannot be
    tested against a provider that answers identically forever.
    """

    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            calls.append("complete")
            index = min(len(calls) - 1, len(texts) - 1)
            return LLMResponse(text=texts[index], model="stub")

    return _LLM()


_OVERSIZE = "The guidance describes the study design " * 60 + "[1]."
_CLEAN = "The guidance recommends a single-dose fasting study [1]."


def _run_with(monkeypatch: pytest.MonkeyPatch, texts: list[str]) -> tuple[Any, list[str]]:
    _seed_corpus(_CORPUS)
    calls: list[str] = []
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _sequence_stub_llm(texts, calls)
    )
    return qa_mod.ask(_QUESTION), calls


def test_the_oversize_fixture_actually_breaches_the_bound() -> None:
    """Guards the other tests: a fixture under the cap would prove nothing."""
    from regwatch.generate import prose_turn as pt

    assert len(_OVERSIZE) > pt.PROSE_MAX_SENTENCE_CHARS
    assert pt.bounds_exceeded(_CLEAN) is None


def test_an_oversize_sentence_is_repaired_in_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prose_mode(monkeypatch)
    result, calls = _run_with(monkeypatch, [_OVERSIZE, _CLEAN])
    assert not result.refused, f"repair did not recover the turn: {result.reason}"
    assert len(calls) == 2, f"expected one repair, saw {len(calls)} completions"


def test_the_repair_is_attempted_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry LOOP on a degenerate model is the failure this must not become."""
    _prose_mode(monkeypatch)
    result, calls = _run_with(monkeypatch, [_OVERSIZE, _OVERSIZE, _CLEAN])
    assert result.refused
    assert len(calls) == 2, f"repair must not loop, saw {len(calls)} completions"


def test_a_still_oversize_turn_declines_with_an_internal_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Greppable in the audit, so this stays separable from parse failures."""
    _prose_mode(monkeypatch)
    result, _ = _run_with(monkeypatch, [_OVERSIZE, _OVERSIZE])
    assert result.refused
    assert result.reason == "oversize_sentence"


def test_the_user_never_sees_the_reason_code_or_any_validation_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2,000-char rule is plumbing; the reply has to sound like a person."""
    _prose_mode(monkeypatch)
    result, _ = _run_with(monkeypatch, [_OVERSIZE, _OVERSIZE])
    lowered = result.answer.lower()
    for banned in ("oversize", "malformed", "structure", "validation", "2000", "character"):
        assert banned not in lowered, f"user-facing text leaked {banned!r}: {result.answer}"
    assert result.answer.strip(), "a decline still has to say something"


def test_v7_repairs_an_oversize_sentence_in_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v7 is what production serves; #187 shipped its #183 tests v6-only."""
    _v7_mode(monkeypatch)
    result, calls = _run_with(monkeypatch, [_OVERSIZE, _CLEAN])
    assert not result.refused, f"v7 repair did not recover the turn: {result.reason}"
    assert len(calls) == 2


def test_v7_declines_conversationally_when_the_repair_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _v7_mode(monkeypatch)
    result, _ = _run_with(monkeypatch, [_OVERSIZE, _OVERSIZE])
    assert result.refused
    assert result.reason == "oversize_sentence"
    assert "oversize" not in result.answer.lower()


def test_a_normal_turn_still_takes_exactly_one_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounds must cost nothing on the 62-of-62 rows that never breach."""
    _prose_mode(monkeypatch)
    result, calls = _run_with(monkeypatch, [_CLEAN])
    assert not result.refused
    assert len(calls) == 1


def test_the_bounds_repair_attempt_is_counted_as_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repaired turn pays for TWO provider round trips; the ledger says so.

    The repair used to run outside the `synthesis` stage, so a breached turn
    reported one completion's worth of synthesis_ms while charging the user for
    two -- and the missing second call landed in the unattributed remainder.
    Both attempts now share the one key (repeats sum) and `counts` records the
    retry, which is what makes an inflated synthesis_ms explainable.
    """
    _v7_mode(monkeypatch)
    result, calls = _run_with(monkeypatch, [_OVERSIZE, _CLEAN])
    assert not result.refused, f"v7 repair did not recover the turn: {result.reason}"
    assert len(calls) == 2
    timings = _only_route_json()["timings"]
    assert timings["counts"]["synthesis"] == 2, timings
