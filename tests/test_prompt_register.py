"""Register and depth rules of the served synthesis prompt (audit RC6 + RC7).

Four things are pinned here, and each one failed a real prod turn before it
was written:

* The v7 system prompt states a length default, an anti-RAG-language rule, a
  conditional hedge and verb fidelity -- and no longer promises structured BE
  data or version history that ``GROUNDED_QA_USER_V7`` never sends.
* The two paragraphs this change does NOT own (the presentation sentence and
  the per-sentence marker paragraph) are asserted verbatim, so an accidental
  edit to either is a test failure rather than a silent policy change.
* The recent-conversation wrapper keeps its literal "Recent conversation"
  label and its byte-identical empty form, because the single-turn prompt is
  what the eval lane measures.
* A drill-down follow-up gets ONE extra user turn asking for depth, and
  nothing else does. Evidence width is untouched by design.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from regwatch.common import conversation as conv
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import prompts
from regwatch.generate.llm import LLMResponse
from tests.test_conversational_memory import _seed_turn
from tests.test_invariants import _meta, _only_route_json, _seed_corpus

pytestmark = pytest.mark.invariants

_CORPUS = [("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))]
_PROSE_ANSWER = "FDA recommends a single-dose fasting study [1]."

# The system prompt is hard-wrapped, so every sentence assertion runs against
# the whitespace-flattened text; a rewrap must not read as a policy change.
_FLAT_V7 = " ".join(prompts.GROUNDED_QA_SYSTEM_V7.split())


def _v7_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flags the turn onto the live v7 prose path and reaches synthesis."""
    monkeypatch.setenv("REGWATCH_PROSE_SYNTHESIS", "1")
    monkeypatch.setenv("REGWATCH_SELECTIVE_CITATION", "1")
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()


class _CapturingLLM:
    """Stub synthesizer that records every message list it is handed."""

    name = "capture"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[Any]] = []

    def complete(self, messages: list[Any], *a: object, **kw: object) -> LLMResponse:
        self.calls.append(list(messages))
        return LLMResponse(text=self.text, model="capture")

    @property
    def last_user_contents(self) -> list[str]:
        return [m.content for m in self.calls[-1] if m.role == "user"]


def _run(
    monkeypatch: pytest.MonkeyPatch, question: str, *, session_id: str | None
) -> tuple[_CapturingLLM, qa_mod.QAResult]:
    _seed_corpus(_CORPUS)
    stub = _CapturingLLM(_PROSE_ANSWER)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)
    result = qa_mod.ask(question, session_id=session_id)
    return stub, result


# ---------- RC6: the register rules are in the served text ----------


@pytest.mark.parametrize(
    "sentence",
    [
        "Default to the shortest answer that fully answers the question, usually two "
        "to four sentences.",
        "Go longer only when the user asks for detail, a comparison, or a walkthrough.",
        "Never refer to passages, context, retrieval or documents you were given; "
        "speak about FDA guidance and about what you know, the way a colleague would.",
        "Keep the source's own verb.",
        "A PSG that recommends has not required; do not upgrade recommends, should, "
        "or may into must or requires.",
        "A sentence that states what a source recommends still carries its marker.",
        "When the evidence is thin or ambiguous, name the uncertainty in one sentence "
        "and the best next source; when it answers the question, answer it and stop.",
    ],
)
def test_v7_system_prompt_carries_each_register_rule(sentence: str) -> None:
    assert sentence in _FLAT_V7


def test_v7_hedge_is_conditional_not_commanded_every_turn() -> None:
    """The first clause is kept verbatim; the per-turn hedge-and-next-step
    imperative that produced a caveat on answered questions is gone."""
    assert "Explain concepts, reason about the evidence, use stable general knowledge." in _FLAT_V7
    assert "name your uncertainty" not in _FLAT_V7
    assert "say what is worth checking next" not in _FLAT_V7


def test_v7_capabilities_line_promises_only_what_the_user_message_carries() -> None:
    """GROUNDED_QA_USER_V7 sends recent context, the question and the passages.

    Promising structured BE data and version history left the model two bad
    options: assert them from memory (and be dropped by the gate) or narrate
    the shortfall at the user.
    """
    assert "structured BE data" not in _FLAT_V7
    assert "document history and version changes" not in _FLAT_V7
    assert "You have access to FDA guidance and PSGs, and the conversation so far." in _FLAT_V7


def test_v7_declines_in_its_own_words_rule_survives() -> None:
    """The conversational decline depends on this line; the anti-RAG rule and
    the conditional hedge must not have displaced it."""
    assert (
        "If the evidence does not cover something, say so plainly and move the work "
        "forward -- name what you do have, and the best next source." in _FLAT_V7
    )


# ---------- RC6: the paragraphs this change does NOT own ----------


def test_presentation_sentence_is_untouched() -> None:
    """Owned by the structured-claims lane."""
    assert (
        "Talk like a capable coworker: direct, concise, practical. Use headings,\n"
        "bullets or tables when they genuinely help." in prompts.GROUNDED_QA_SYSTEM_V7
    )


def test_per_sentence_marker_paragraph_is_untouched() -> None:
    """Load-bearing until the gate stops treating a missed marker as fatal;
    removing it re-opens the citation-placement regression (prod #2499)."""
    assert (
        "Put the marker at the end of EACH sentence that states a source fact, not at\n"
        "the end of a paragraph or a bullet group. Sentences are admitted one at a\n"
        "time, so a sentence without its own marker is dropped even when the sentence\n"
        "after it carries one. Repeat the same number as often as you need."
        in prompts.GROUNDED_QA_SYSTEM_V7
    )


def test_v7_identity_stays_version_7() -> None:
    """The sha256 moves with the text on its own (identify_prompt hashes the
    template), so audit artifacts already distinguish the new register; the
    version literal is the POLICY generation and this is not one."""
    assert prompts.GROUNDED_QA_PROMPT_V7.version == "7"
    assert prompts.GROUNDED_QA_PROMPT_V7.sha256 != prompts.GROUNDED_QA_PROMPT_V6.sha256


# ---------- RC6: the recent-conversation wrapper ----------


def test_single_turn_prompt_is_byte_identical_to_the_no_history_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _v7_mode(monkeypatch)
    stub, result = _run(monkeypatch, "What study design is recommended?", session_id=None)
    prompt = stub.last_user_contents[0]
    assert "Recent conversation" not in prompt
    assert prompt.startswith("<untrusted_question>\n")
    assert result.status == "answer"


def test_history_wrapper_invites_building_on_the_thread_and_still_forbids_citing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _v7_mode(monkeypatch)
    sid = "sess-register"
    conv.ensure_session(sid)
    _seed_turn(sid, q="What study design?", a="A fasting study is recommended.", order=1)
    stub, _ = _run(monkeypatch, "What about the fed study?", session_id=sid)
    recent = stub.last_user_contents[0].split("<untrusted_question>", 1)[0]
    # The literal label is asserted by tests/test_conversational_memory.py too.
    assert recent.startswith("Recent conversation (")
    assert "you may build on it and need not repeat it" in recent
    assert "never cite it, and never restate it as something FDA said" in recent
    assert "<untrusted_recent_conversation>" in recent
    assert "</untrusted_recent_conversation>" in recent


# ---------- RC7: the depth turn ----------


def test_depth_words_are_a_subset_of_the_drill_down_vocabulary() -> None:
    """The trigger is narrower than _DRILL_DOWN_WORDS on purpose (that set
    includes bare back-references like "it"), but never wider: a word that is
    not a drill-down word has no business asking for depth."""
    assert qa_mod._DEPTH_REQUEST_WORDS <= qa_mod._DRILL_DOWN_WORDS


def test_drill_down_follow_up_appends_one_depth_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _v7_mode(monkeypatch)
    sid = "sess-depth"
    conv.ensure_session(sid)
    _seed_turn(sid, q="What study design?", a="A fasting study is recommended.", order=1)
    stub, result = _run(monkeypatch, "Tell me more about the fed arm.", session_id=sid)
    assert result.status == "answer"
    assert stub.last_user_contents[-1] == qa_mod._DEPTH_TURN.content
    # Exactly one, and it lands AFTER the question+passages turn.
    assert len(stub.last_user_contents) == 2
    assert _only_route_json()["synthesis"]["depth_turn"] is True


def test_first_turn_asking_for_more_gets_no_depth_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """No prior answer exists to go deeper on, so the ask would be a lie."""
    _v7_mode(monkeypatch)
    stub, _ = _run(monkeypatch, "Tell me more about the fed arm.", session_id=None)
    assert qa_mod._DEPTH_TURN.content not in stub.last_user_contents
    assert "depth_turn" not in _only_route_json()["synthesis"]


def test_non_drill_down_follow_up_gets_no_depth_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "What about the fed study?" continues the thread; it does not ask the
    previous answer to grow, and answering it at length would fight the
    shortest-answer default."""
    _v7_mode(monkeypatch)
    sid = "sess-plain"
    conv.ensure_session(sid)
    _seed_turn(sid, q="What study design?", a="A fasting study is recommended.", order=1)
    stub, _ = _run(monkeypatch, "What about the fed study?", session_id=sid)
    assert qa_mod._DEPTH_TURN.content not in stub.last_user_contents
    assert "depth_turn" not in _only_route_json()["synthesis"]


def test_depth_predicate_needs_both_halves() -> None:
    assert qa_mod._asks_for_more_depth("tell me more about the fed arm")
    assert qa_mod._asks_for_more_depth("can you expand on that?")
    # Follow-up shaped, but asks for a fact, not for more of the answer.
    assert not qa_mod._asks_for_more_depth("is it fasting?")
    # Asks for detail, but is a standalone question, not a drill-down.
    assert not qa_mod._asks_for_more_depth(
        "What dissolution details does the albuterol sulfate PSG give?"
    )


# ---------- RC6 item 7: the flag state is versioned, not just secret-held ----------


def test_prod_fly_toml_pins_both_v7_flags() -> None:
    """An unset or cleared Fly secret used to silently reinstate the v6
    cite-or-refuse prompt: a policy rollback with no deploy and no diff. A
    secret of the same name still overrides [env], so instant rollback works.
    """
    fly_toml = Path(__file__).resolve().parents[1] / "fly.toml"
    cfg = tomllib.loads(fly_toml.read_text(encoding="utf-8"))
    assert cfg["env"].get("REGWATCH_PROSE_SYNTHESIS") == "true"
    assert cfg["env"].get("REGWATCH_SELECTIVE_CITATION") == "true"


def test_code_defaults_stay_off() -> None:
    """The eval lane and the ledger pin the flag-off bytes; prod pins the flags
    in fly.toml instead."""
    import config.settings as cs

    fields = cs.Settings.model_fields
    assert fields["prose_synthesis_enabled"].default is False
    assert fields["selective_citation_enabled"].default is False


# ---------- RC6 item 7: the boot line ----------


def test_boot_logs_the_active_qa_prompt_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Which prompt a machine serves is a flag read with no deploy behind it;
    without this line the answer policy in force is only inferable from audit
    rows after the fact."""
    from fastapi.testclient import TestClient

    from regwatch.api import main as main_mod

    _v7_mode(monkeypatch)
    events: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        def info(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

        def warning(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

    monkeypatch.setattr(main_mod, "log", _Recorder())
    with TestClient(main_mod.app):
        pass

    emitted = [kw for event, kw in events if event == "qa_prompt_active"]
    assert len(emitted) == 1
    fields = emitted[0]
    assert fields["prose"] is True
    assert fields["selective"] is True
    assert fields["prompt_version"] == "7"
    assert fields["prompt_sha256"] == prompts.GROUNDED_QA_PROMPT_V7.sha256
