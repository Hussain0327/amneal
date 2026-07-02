"""B2: an LLM-provider failure degrades to a graceful, AUDITED refusal.

The synthesizer is a single external dependency. When it errors (timeout / 429 /
5xx), /query must not return a naked 500 with no audit row — that would break the
"every interaction is audited" guarantee (INV-6) exactly when the system
misbehaves. The turn is refused with status="error" and still written to
query_log, mirroring the deterministic refusal paths.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMStreamChunk
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks


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


class _MidStreamBoomProvider:
    """stream() drops mid-answer AFTER emitting real tokens — the realistic prod
    failure (OpenAI SSE stream cut off). complete() must not be reached: with
    on_token set, ask() takes the stream branch."""

    name = "boom-stream"

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ask() must use stream() when on_token is provided")

    def stream(self, *args: Any, **kwargs: Any) -> Iterator[LLMStreamChunk]:
        yield LLMStreamChunk(delta="A fasting ")
        raise RuntimeError("simulated stream dropped mid-answer")


def test_streaming_provider_error_mid_stream_degrades_to_audited_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that raises after tokens were already painted must land in the
    same B2 audited status='error' refusal as a buffered failure — the try in
    ask() has to cover _stream_synthesis, not just provider.complete."""
    _seed_corpus()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _MidStreamBoomProvider())

    tokens: list[str] = []
    result = qa_mod.ask("What study design is recommended?", on_token=tokens.append)

    # The provisional draft did stream before the drop (diverges from the
    # refusal sentinel, so the guard released it)...
    assert tokens == ["A fasting "]
    # ...but the recorded result is the refusal frame, never the partial answer.
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
