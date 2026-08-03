"""B2: an LLM-provider failure degrades to a graceful, AUDITED refusal.

The synthesizer is a single external dependency. When it errors (timeout / 429 /
5xx), /query must not return a naked 500 with no audit row — that would break the
"every interaction is audited" guarantee (INV-6) exactly when the system
misbehaves. The turn is refused with status="error" and still written to
query_log, mirroring the deterministic refusal paths.

The same boundary now also owns what the reader SEES. Live token streaming from
synthesis is gone: the optional ``on_token`` sink replays the rendered, gated
answer AFTER the audit write and only on an answer/summary turn. So a failed
turn owes the reader zero bytes, and the tests below pin both halves -- silence
on failure, and (the positive control) gated post-audit bytes on success.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import DatabricksProvider
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks
from tests.test_query_stream import _turn_json, _turn_llm


def _seed_corpus() -> None:
    """Seed enough that retrieval clears the refusal threshold and reaches the
    synthesizer (mirrors tests/test_grounded_qa_citations.py)."""
    init_db()
    embedder = get_embedding_provider()
    texts = [
        "Fasting bioequivalence study with 36 subjects.",
        "Dissolution: USP Apparatus 2 at 50 rpm.",
    ]
    base = {
        "doc_id": 1,
        "version_id": 10,
        "section_path": "II.A",
        "normalized_name": "albuterol sulfate",
        "dosage_form": "Aerosol, Metered",
        "route": "Inhalation",
        "source_url": "http://example/PSG_020503.pdf",
        "psg_type": "draft",
        "appl_no": "020503",
    }
    add_chunks(
        ids=["chunk-0", "chunk-1"],
        embeddings=embedder.embed(texts),
        documents=texts,
        metadatas=[dict(base, page=3), dict(base, page=4)],
    )


class _BoomProvider:
    name = "boom"

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated openai 503")


def test_provider_error_degrades_to_audited_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _BoomProvider())

    result = qa_mod.ask("What study design is recommended?")

    # Graceful, non-fabricated refusal — never the raw provider error, never a 500.
    assert result.refused is True
    assert result.status == "error"
    assert result.citations == []
    assert "temporarily unavailable" in result.answer

    # INV-6: the turn is still audited even though the LLM call failed.
    assert result.audit_id is not None
    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.status == "error"
        assert row.refused is True


class _StreamTrapBoomProvider:
    """complete() fails; stream() is a TRAP that must never be entered.

    Two properties in one provider.

    1. Structured synthesis is BUFFERED-ONLY. ``stream()`` carries no
       ``response_format`` anywhere in the provider stack, so a streamed
       synthesis would hand unstructured PROSE to a caller that parses a JSON
       turn -- the failure would look like a malformed-structure machine fault
       forever. Reaching stream() from the QA path is a bug, not a fallback.
    2. A transport failure still degrades to the B2 audited status="error"
       refusal, exactly as the buffered case above.
    """

    name = "boom-stream-trap"

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated provider drop mid-answer")

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("structured synthesis must never call provider.stream()")


def test_provider_error_with_token_sink_emits_nothing_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider failure on a turn that HAS a live token sink attached must
    paint zero bytes and land in the same B2 audited status='error' refusal.

    This replaces the old mid-stream-drop test. Live synthesis streaming is
    gone: ``on_token`` now replays the rendered answer AFTER the audit write and
    only on an answer/summary turn, so a failed turn owes the reader nothing.
    That is strictly stronger than what this test used to assert -- it used to
    require that a partial provisional draft ('A fasting ') HAD reached the
    reader before the drop.
    """
    _seed_corpus()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _StreamTrapBoomProvider())

    tokens: list[str] = []
    result = qa_mod.ask("What study design is recommended?", on_token=tokens.append)

    # No ungated byte reaches the reader: a failed turn streams nothing at all.
    assert tokens == []
    # The recorded result is the service-error refusal, never a partial answer.
    assert result.refused is True
    assert result.status == "error"
    assert result.citations == []
    assert "temporarily unavailable" in result.answer

    # INV-6: the failed turn is still audited.
    assert result.audit_id is not None
    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.status == "error"
        assert row.refused is True


def test_token_sink_only_ever_gets_gated_already_audited_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control for the test above.

    Without it ``tokens == []`` on the failure path would also pass if the token
    sink were dead code. Here a GOOD turn proves the sink fires, that every byte
    it receives is the RENDERED (gate-admitted) answer -- not the model's raw
    JSON draft -- and that the first byte arrives only AFTER the audit row is
    committed. That ordering is the whole point of moving the replay out of the
    synthesis loop: no user-visible byte is ever un-gated or un-audited.
    """
    _seed_corpus()
    turn = _turn_json(("A fasting bioequivalence study is recommended", [("PSG_020503", 3)]))
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _turn_llm(turn))

    def _rows() -> int:
        with session_scope() as s:
            return int(s.scalar(select(func.count()).select_from(QueryLog)) or 0)

    before = _rows()
    tokens: list[str] = []
    audited_at_first_token: list[bool] = []

    def sink(delta: str) -> None:
        if not tokens:
            audited_at_first_token.append(_rows() > before)
        tokens.append(delta)

    result = qa_mod.ask("What study design is recommended?", on_token=sink)

    assert result.refused is False
    assert result.status == "answer"
    assert tokens, "an answer turn must replay its rendered answer to the sink"
    assert "".join(tokens) == result.answer
    assert turn not in "".join(tokens)  # never the raw model draft
    assert audited_at_first_token == [True]


def _d1_armed_provider() -> DatabricksProvider:
    """A real DatabricksProvider whose endpoint reports an off-perimeter model.

    The configured name is allowlisted (as the boot validator demands), so only
    the runtime served-model check can catch this.
    """
    response = SimpleNamespace(
        id="db-response",
        object="chat.completion",
        created=1,
        model="databricks-claude-sonnet-5",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="An answer [PSG_020503, p.3]."),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response))
    )
    return DatabricksProvider(
        model="workspace.default.regwatch",
        base_url="https://workspace.example/serving-endpoints",
        token="token",
        role="synthesizer",
        d1_enforced=True,
        d1_allowed_models=("workspace.default.regwatch",),
        client=client,
    )


def test_d1_violation_degrades_to_an_audited_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A residency failure must land in the SAME audited boundary as a transport
    failure: refuse the answer, keep the turn on the record (INV-6), and never
    leak the exception text (which names models, not prompts) to the user."""
    _seed_corpus()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _d1_armed_provider())

    result = qa_mod.ask("What study design is recommended?")

    assert result.refused is True
    assert result.status == "error"
    assert result.citations == []
    assert "temporarily unavailable" in result.answer
    # The off-perimeter answer is never served, and the guard's own message
    # (which names the served model) never reaches the analyst.
    assert "PSG_020503" not in result.answer
    assert "databricks-claude-sonnet-5" not in result.answer

    assert result.audit_id is not None
    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.status == "error"
        assert row.refused is True
