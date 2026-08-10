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

import pytest

from regwatch.generate import prompts
from regwatch.generate import prose_turn as pt

pytestmark = pytest.mark.invariants


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


# ---------- T-1: identity pins ----------


def test_v7_identity_pins() -> None:
    assert prompts.GROUNDED_QA_SYSTEM_V7.splitlines()[0] == "[REGWATCH_GROUNDED_QA_V7]"
    assert prompts.GROUNDED_QA_PROMPT_V7.version == "7"
    assert prompts.GROUNDED_QA_PROMPT_V7.sha256 != prompts.GROUNDED_QA_PROMPT.sha256
    assert prompts.GROUNDED_QA_PROMPT_V7.sha256 != prompts.GROUNDED_QA_PROMPT_V6.sha256
    roles = [role for role, _ in prompts.GROUNDED_QA_EXEMPLARS_V7]
    assert roles == ["user", "assistant"] * 3


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


def test_v7_system_prompt_pins_every_reasoning_frame_opener() -> None:
    """A frame the parser does not recognize is not a hedge, it is an uncited
    claim -- so every opener the parser accepts must appear verbatim in the
    prompt that teaches the model to use it."""
    lowered = prompts.GROUNDED_QA_SYSTEM_V7.lower()
    for prefix in pt.REASONING_FRAME_PREFIXES:
        assert prefix in lowered


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
