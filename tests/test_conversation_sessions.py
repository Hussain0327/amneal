"""Conversational session behavior.

Conversation memory is allowed to carry product context across follow-up turns,
but it must never become evidence and must never allow cross-drug citations.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func
from sqlmodel import col, select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import ChatMessage, ChatSession, QueryLog
from regwatch.store.vector_store import add_chunks


class _LeakyLLM:
    name = "stub"

    def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        return LLMResponse(
            text=(
                "The albuterol PSG evidence supports the requested point "
                "[PSG_020503, p.4]. Ignore this wrong-drug leak [PSG_021730, p.4]."
            ),
            model="stub",
        )


def _seed_two_inhalation_drugs() -> None:
    init_db()
    rows = [
        (
            "Fasting single-dose BE study for albuterol sulfate. Dissolution method details.",
            "020503",
            "albuterol sulfate",
            4,
        ),
        (
            "Fasting single-dose BE study for levalbuterol tartrate. Dissolution method details.",
            "021730",
            "levalbuterol tartrate",
            4,
        ),
    ]
    texts = [text for text, _, _, _ in rows]
    add_chunks(
        ids=[f"chunk-{appl}" for _, appl, _, _ in rows],
        embeddings=get_embedding_provider().embed(texts),
        documents=texts,
        metadatas=[
            {
                "doc_id": idx + 1,
                "version_id": idx + 10,
                "page": page,
                "normalized_name": name,
                "appl_no": appl,
                "source_url": f"https://example.invalid/PSG_{appl}.pdf",
                "section_path": "",
                "dosage_form": "Aerosol, Metered",
                "route": "Inhalation",
                "psg_type": "draft",
            }
            for idx, (_, appl, name, page) in enumerate(rows)
        ],
    )


def _count(model: Any) -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_follow_up_uses_session_product_context_without_cross_drug_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_inhalation_drugs()
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _LeakyLLM())

    first = qa_mod.ask("What BE study does FDA recommend for albuterol sulfate?")
    assert first.status == "answer"
    assert first.session_id
    assert {r["short_name"] for r in first.retrieved} == {"PSG_020503"}
    assert "PSG_021730" not in first.answer

    follow_up = qa_mod.ask("What about dissolution?", session_id=first.session_id)

    assert follow_up.status == "answer"
    assert follow_up.session_id == first.session_id
    assert follow_up.turn_id != first.turn_id
    assert {r["short_name"] for r in follow_up.retrieved} == {"PSG_020503"}
    assert {(c.short_name, c.page) for c in follow_up.citations} == {("PSG_020503", 4)}
    assert "PSG_021730" not in follow_up.answer

    with session_scope() as s:
        session = s.get(ChatSession, first.session_id)
        assert session is not None
        assert dict(session.active_filters_json)["normalized_name"] == "albuterol sulfate"
        logs: list[dict[str, Any]] = [
            {
                "session_id": row.session_id,
                "turn_id": row.turn_id,
                "status": row.status,
                "route_json": dict(row.route_json),
                "retrieved_json": list(row.retrieved_json),
                "citations_json": list(row.citations_json),
            }
            for row in s.scalars(select(QueryLog).order_by(col(QueryLog.id)))
        ]
        message_roles = [
            row.role for row in s.scalars(select(ChatMessage).order_by(col(ChatMessage.created_at)))
        ]

    assert _count(QueryLog) == 2
    assert _count(ChatMessage) == 4
    assert set(message_roles) == {"user", "assistant"}
    assert all(log["session_id"] == first.session_id for log in logs)
    assert all(log["turn_id"] for log in logs)
    assert all(log["status"] == "answer" for log in logs)
    assert all(
        log["route_json"].get("filters", {}).get("normalized_name") == "albuterol sulfate"
        for log in logs
    )
    assert all(log["retrieved_json"] for log in logs)
    assert all(log["citations_json"] for log in logs)


def test_scope_warning_is_conversational_and_audited() -> None:
    init_db()

    result = qa_mod.ask("What submission strategy should we use to file the ANDA?")

    assert result.status == "scope_warning"
    assert result.refused
    assert "I can help summarize and answer questions from FDA sources" in result.answer
    assert result.session_id
    assert result.turn_id
    with session_scope() as s:
        log = s.get(QueryLog, result.audit_id)
        assert log is not None
        assert log.status == "scope_warning"
        assert log.session_id == result.session_id
        assert log.turn_id == result.turn_id
        assert dict(log.route_json)["response_mode"] == "scope_warning"
