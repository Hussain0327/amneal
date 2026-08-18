"""The terminal /query result IS the turn gate's rendering -- pinned on the wire.

grounded_qa captures the model's raw draft, but the response carries
``rendered_answer = tg.render_answer(admitted)``: the wire answer can never be
the raw draft. The gate-level halves of this contract live in
tests/test_turn_gate.py and tests/test_invariants.py (ask() level); these two
tests pin the SAME property at the HTTP surface (QueryResponse.answer over
POST /query), where a future handler returning the draft -- or the
synthesizer's JSON envelope -- verbatim would pass every gate-level test and
still leak ungated content to clients.
"""

from __future__ import annotations

import pytest
from config.settings import get_settings

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import turn_gate as tg
from tests.conftest import AuthedClient
from tests.test_invariants import _meta, _seed_corpus, _stub_llm, _turn

_QUESTION = "What study design is recommended?"
# Draft-only content: the claim below cites a document that was never
# retrieved, so the gate must strip it (partial) or decline the turn (all
# claims dropped). Its text may then never appear anywhere in the wire body.
_FABRICATED_CLAIM = ("The agency also recommends an in vivo fed study.", [("PSG_999999", 7)])


def test_wire_answer_is_the_gate_rendering_not_the_raw_draft(
    auth_client: AuthedClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stripped draft claim leaves nothing behind in the /query response."""
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3))])
    completion = _turn(
        [
            (
                "A fasting bioequivalence study with 36 subjects is recommended.",
                [("PSG_020503", 3)],
            ),
            _FABRICATED_CLAIM,
        ]
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(completion))

    response = auth_client.post("/query", json={"question": _QUESTION})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answer"
    # The admitted claim arrives RENDERED: the renderer authors the marker.
    assert (
        "A fasting bioequivalence study with 36 subjects is recommended "
        "[PSG_020503, p.3]." in body["answer"]
    )
    # The user is told something was removed, in the rendering's plain words.
    assert tg.PARTIAL_DROP_DISCLOSURE in body["answer"]
    # The stripped claim left nothing on the wire: not its text, not its
    # fabricated marker -- anywhere in the body, not just in `answer` (the
    # draft is audit-row telemetry, never wire content).
    assert "fed study" not in response.text
    assert "PSG_999999" not in response.text
    # No raw-draft artifact leaks either: the synthesizer emits a JSON claims
    # envelope, which must never surface as the answer.
    assert '"claims"' not in body["answer"]


def test_wire_answer_on_a_gate_decline_is_the_refusal_never_the_draft(
    auth_client: AuthedClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the gate declines the whole turn, the wire carries the refusal."""
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3))])
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_turn([_FABRICATED_CLAIM]))
    )

    response = auth_client.post("/query", json={"question": _QUESTION})
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is True
    assert body["status"] == "refused"
    assert body["reason"] == "no_valid_citations"
    # The gated rendering of a declined turn is the fixed refusal copy...
    assert body["answer"] == get_settings().refusal_text
    assert body["citations"] == []
    # ...and the raw draft's content is absent from the whole wire body.
    assert "fed study" not in response.text
    assert "PSG_999999" not in response.text
