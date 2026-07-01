"""Multi-turn conversational memory — ergonomic follow-ups WITHOUT an INV-1 leak.

The synthesizer now sees the last few ANSWERED turns as a labeled, citation-free
"Recent conversation" block, so a follow-up ("what about the fed study?") resolves
naturally. These tests pin the load-bearing property: prior-turn text is context
only, never a source. A fact that lived solely in a prior turn cannot acquire a
valid citation this turn — it is stripped and the answer refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from config.settings import get_settings

from regwatch.common import conversation as conv
from regwatch.common.citations import has_citation
from regwatch.common.conversation import get_recent_turns
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import ChatMessage
from tests.test_invariants import _meta, _seed_corpus, _stub_llm

pytestmark = pytest.mark.invariants


def _seed_turn(
    session_id: str, *, q: str, a: str, status: str | None = "answer", order: int
) -> str:
    """Insert a completed (user, assistant) turn with explicit, ordered timestamps."""
    tid = f"turn-{order}"
    base = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=order)
    with session_scope() as s:
        s.add(
            ChatMessage(
                id=str(uuid4()),
                session_id=session_id,
                turn_id=tid,
                role="user",
                content=q,
                created_at=base,
            )
        )
        s.add(
            ChatMessage(
                id=str(uuid4()),
                session_id=session_id,
                turn_id=tid,
                role="assistant",
                content=a,
                status=status,
                created_at=base + timedelta(seconds=1),
            )
        )
    return tid


class _CapturingLLM:
    """Stub synthesizer that records the user prompt it was handed."""

    name = "capture"

    def __init__(self, text: str) -> None:
        self.text = text
        self.user_prompts: list[str] = []

    def complete(self, messages: list, *a: object, **kw: object) -> LLMResponse:
        self.user_prompts.append(next((m.content for m in messages if m.role == "user"), ""))
        return LLMResponse(text=self.text, model="capture")


# ---------- get_recent_turns unit behavior ----------


def test_get_recent_turns_pairs_orders_and_limits() -> None:
    init_db()
    sid = "sess-order"
    conv.ensure_session(sid)
    for i in range(1, 5):
        _seed_turn(sid, q=f"Q{i}", a=f"A{i}", order=i)
    turns = get_recent_turns(sid, limit=3)
    # The last 3 completed turns, oldest-first.
    assert [t.question for t in turns] == ["Q2", "Q3", "Q4"]
    assert [t.answer for t in turns] == ["A2", "A3", "A4"]


def test_get_recent_turns_excludes_current_and_non_answers() -> None:
    init_db()
    sid = "sess-filter"
    conv.ensure_session(sid)
    _seed_turn(sid, q="Q1", a="A1", status="answer", order=1)
    _seed_turn(sid, q="Q2", a="declined", status="refused", order=2)
    _seed_turn(sid, q="Q3", a="C3", status="clarify", order=3)
    _seed_turn(sid, q="Q4", a="A4", status="summary", order=4)
    current = _seed_turn(sid, q="CURRENT", a="unused", status="answer", order=5)
    turns = get_recent_turns(sid, limit=9, exclude_turn_id=current)
    # refused + clarify dropped; current excluded; answer + summary kept, oldest-first.
    assert [t.question for t in turns] == ["Q1", "Q4"]


def test_get_recent_turns_empty_and_bad_inputs() -> None:
    init_db()
    assert get_recent_turns(None) == []
    assert get_recent_turns("does-not-exist") == []
    conv.ensure_session("sess-empty")
    assert get_recent_turns("sess-empty", limit=0) == []


# ---------- prompt shaping ----------


def test_single_turn_prompt_has_no_recent_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no history the user prompt is byte-identical to the single-turn form —
    it starts with the question and carries no conversation scaffolding."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    stub = _CapturingLLM("A fasting study is recommended [PSG_020503, p.3].")
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)
    qa_mod.ask("What study design is recommended?")
    prompt = stub.user_prompts[-1]
    assert "Recent conversation" not in prompt
    assert prompt.startswith("Question:")


def test_followup_prompt_threads_prior_turn_without_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up carries the prior turn's PROSE as context, but its citation
    markers are stripped so the model cannot parrot a stale [PSG, p.N]."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    sid = "sess-ctx"
    conv.ensure_session(sid)
    _seed_turn(
        sid,
        # Markers on BOTH sides — a citation-shaped token in the prior QUESTION
        # must be stripped too, not just the answer's.
        q="What did [PSG_020503, p.9] recommend for the BE study?",
        a="A fed study is also recommended [PSG_020503, p.9].",
        status="answer",
        order=1,
    )
    stub = _CapturingLLM("A fasting study is recommended [PSG_020503, p.3].")
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)
    qa_mod.ask("What study design is recommended?", session_id=sid)
    prompt = stub.user_prompts[-1]
    recent_part = prompt.split("Question:")[0]
    assert "Recent conversation" in recent_part
    assert "fed study is also recommended" in recent_part  # answer prose threaded
    assert "recommend for the BE study" in recent_part  # question prose threaded
    assert not has_citation(recent_part)  # markers stripped from BOTH sides


# ---------- the load-bearing INV-1 property under multi-turn ----------


def test_followup_stale_prior_citation_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A follow-up answer that re-cites a passage from a PRIOR turn which is NOT in
    the current retrieval set is stripped to a refusal: a fact that lived only in a
    prior turn cannot acquire a valid citation now (INV-1 holds under memory)."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    sid = "sess-inv1"
    conv.ensure_session(sid)
    _seed_turn(
        sid,
        q="What BE study for albuterol?",
        a="A fed study is recommended [PSG_020503, p.9].",
        status="answer",
        order=1,
    )
    # Adversarial: the model parrots the prior fed-study fact, citing the PRIOR
    # page (p.9) — which is not among the current passages (only p.3 was retrieved).
    parroted = "A fed study is recommended [PSG_020503, p.9]."
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(parroted))
    result = qa_mod.ask("What study design is recommended?", session_id=sid)
    assert result.refused
    assert "p.9" not in result.answer
    assert result.answer == get_settings().refusal_text
