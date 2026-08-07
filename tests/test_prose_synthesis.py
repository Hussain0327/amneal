"""v6 prose synthesis (REGWATCH_PROSE_SYNTHESIS) end-to-end through ask().

Same refuse-or-cite POLICY as v5, prose + [n] FORMAT: these tests drive the
full flag-on chain -- v6 prompt build, prose parse, the claims bridge, gate
admission with re-stamp correction live and the uncited-downgrade path OFF --
over the real retrieval stack, with the echo provider or a fixed stub.

The flag-off suite (everything else in tests/) is the byte-identity proof for
v5; nothing here runs with the flag off except the identity pins.
"""

from __future__ import annotations

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
