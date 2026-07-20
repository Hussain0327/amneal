"""Ask Tier-2 wire enrichment + history persistence.

Covers the three Tier-2 backend changes, each asserting behavior that breaks if
the change is reverted:

  1. CONFIDENCE — a cited answer's wire citations carry the matching retrieved
     passage's score (copied by chunk_id, never recomputed).
  2. RECENCY — wire citations carry recommended_date + diff_summary joined from
     psg_version/psg_document; a DB failure degrades to null and never raises.
  3. HISTORY — reason / interpretation / audit_id / clarify[] / related[] are
     persisted on chat messages and returned by GET /sessions/{id}, so a
     reloaded turn keeps its provenance and next-step affordances.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import col, select

from regwatch.api import main as api_main
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store import queries as queries_mod
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import ChatMessage, PsgDocument, PsgVersion
from regwatch.store.queries import fetch_citation_recency
from regwatch.store.vector_store import add_chunks
from tests.conftest import create_user, session_client


def _stub_llm(text: str) -> Any:
    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    return _LLM()


def _meta(doc_id: int, page: int, short: str = "PSG_020503") -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "version_id": doc_id * 10,
        "page": page,
        "section_path": "II.A",
        "normalized_name": "albuterol sulfate",
        "dosage_form": "Aerosol, Metered",
        "route": "Inhalation",
        "source_url": f"http://example/{short}.pdf",
        "psg_type": "draft",
        "appl_no": short.replace("PSG_", ""),
    }


def _seed_corpus(passages_with_meta: list[tuple[str, dict[str, Any]]]) -> None:
    init_db()
    embedder = get_embedding_provider()
    texts = [p for p, _ in passages_with_meta]
    embeddings = embedder.embed(texts)
    ids = [f"chunk-{i}" for i in range(len(texts))]
    metas = [m for _, m in passages_with_meta]
    add_chunks(ids=ids, embeddings=embeddings, documents=texts, metadatas=metas)


def _seed_psg_rows(*, doc_id: int, version_id: int, recommended: str, diff: str) -> None:
    """Persist the structured PSG rows the recency join reads (matching _meta)."""
    with session_scope() as s:
        s.add(
            PsgDocument(
                id=doc_id,
                active_ingredient="albuterol sulfate",
                normalized_name="albuterol sulfate",
                dosage_form="Aerosol, Metered",
                route="Inhalation",
                psg_type="draft",
                recommended_date="2019-01-01",  # doc-level fallback (older)
                source_url="http://example/PSG_020503.pdf",
                content_hash="hash-doc",
            )
        )
        s.add(
            PsgVersion(
                id=version_id,
                psg_document_id=doc_id,
                content_hash="hash-ver",
                recommended_date=recommended,
                diff_summary=diff,
            )
        )


# ---------- 1) CONFIDENCE: score on the wire ----------


def test_wire_citation_carries_retrieved_score(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("A fasting study [PSG_020503, p.3].")
    )
    result = qa_mod.ask("What study design is recommended?")
    assert not result.refused
    assert result.citations, "expected a cited answer"

    # The score the wire serializer must surface == the audited retrieval score
    # for the same chunk_id (copied, not recomputed).
    by_chunk = {r["chunk_id"]: r["score"] for r in result.retrieved}
    wire = api_main._wire_citations(result)
    assert wire, "expected wire citations"
    for c in wire:
        assert c.chunk_id in by_chunk
        assert c.score == by_chunk[c.chunk_id]
        assert c.score is not None


# ---------- 2) RECENCY: join + failure path ----------


def test_wire_citation_carries_recommended_date_and_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    # version_id mirrors _meta (doc_id * 10).
    _seed_psg_rows(doc_id=1, version_id=10, recommended="2023-07-15", diff="Tightened Cmax band.")
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("A fasting study [PSG_020503, p.3].")
    )
    result = qa_mod.ask("What study design is recommended?")
    assert result.citations

    wire = api_main._wire_citations(result)
    c = wire[0]
    assert c.recommended_date is not None
    assert c.recommended_date.isoformat() == "2023-07-15"  # version wins over doc fallback
    assert c.diff_summary == "Tightened Cmax band."


def test_recency_falls_back_to_document_date() -> None:
    # version row exists but its recommended_date is null -> use the document's.
    init_db()
    with session_scope() as s:
        s.add(
            PsgDocument(
                id=5,
                active_ingredient="x",
                normalized_name="x",
                psg_type="final",
                recommended_date="2018-03-03",
                source_url="http://example/x.pdf",
                content_hash="h",
            )
        )
        s.add(PsgVersion(id=50, psg_document_id=5, content_hash="h2", recommended_date=None))
    idx = fetch_citation_recency(version_ids=[50], doc_ids=[5])
    resolved = idx.resolve(version_id=50, doc_id=5)
    assert resolved.recommended_date == "2018-03-03"
    assert resolved.diff_summary is None


def test_recency_join_failure_returns_empty_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()

    def _boom(*a: object, **k: object) -> Any:
        raise RuntimeError("simulated DB outage / timeout")

    # Force the batched lookup's DB access to fail; it must swallow + return empty.
    monkeypatch.setattr(queries_mod, "session_scope", _boom)
    idx = fetch_citation_recency(version_ids=[10], doc_ids=[1])
    assert idx.by_version == {}
    assert idx.doc_dates == {}
    # resolve() on the empty index yields all-null, never raises.
    resolved = idx.resolve(version_id=10, doc_id=1)
    assert resolved.recommended_date is None
    assert resolved.diff_summary is None


def test_query_does_not_block_when_recency_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB failure under the recency join must degrade to null, not break the
    answer: the validated citation (and its score) still serializes."""
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("A fasting study [PSG_020503, p.3].")
    )
    result = qa_mod.ask("What study design is recommended?")
    assert result.citations

    # Break the recency lookup's DB access at the source; the store helper's own
    # try/except converts it to an empty index, so _wire_citations never raises.
    def _boom(*a: object, **k: object) -> Any:
        raise RuntimeError("simulated DB outage / timeout")

    monkeypatch.setattr(queries_mod, "session_scope", _boom)
    wire = api_main._wire_citations(result)
    assert wire[0].recommended_date is None
    assert wire[0].diff_summary is None
    assert wire[0].score is not None  # score still present (it comes from retrieval, not the DB)


# ---------- 3) HISTORY: reason / interpretation / audit_id / clarify / related ----------


def test_refusal_round_trips_reason_and_related(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal turn persists reason + related (next-step pointers) and GET
    /sessions/{id} returns them — so a reloaded refusal keeps its affordances.

    Persist a refusal-shaped assistant message directly (deterministic, no LLM)
    so the related[] payload is non-empty and the round-trip is exact; the empty
    refusal contract (citations []) is preserved alongside it.
    """
    from regwatch.common.conversation import ensure_session, record_message

    user_id = create_user()
    client: TestClient = session_client(user_id)
    try:
        session_id = ensure_session(user_id=str(user_id))
        related = [
            {"label": "Levalbuterol tartrate", "query": "levalbuterol tartrate", "filters": None}
        ]
        record_message(
            session_id=session_id,
            turn_id="t-refusal",
            role="assistant",
            content="I couldn't find that exact drug.",
            status="refused",
            reason="did_you_mean",
            interpretation="I couldn't find that exact drug. Did you mean:",
            citations=[],
            related=related,
        )

        # Persistence is the Python-side contract now (the /sessions WIRE shape
        # is pinned by Go's TestSessionDetailShapeAndVerbatimPassthrough); read
        # the stored rows directly.
        with session_scope() as s:
            persisted = [
                {
                    "role": m.role,
                    "reason": m.reason,
                    "interpretation": m.interpretation,
                    "related": m.related_json,
                    "citations": m.citations_json,
                    "clarify": m.clarify_json,
                }
                for m in s.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(col(ChatMessage.created_at).asc())
                )
            ]
        assistant_rows = [m for m in persisted if m["role"] == "assistant"]
        assert assistant_rows, "assistant turn must be persisted"
        a = assistant_rows[-1]
        assert a["reason"] == "did_you_mean"
        assert a["interpretation"] == "I couldn't find that exact drug. Did you mean:"
        assert a["related"] == related
        assert a["citations"] == []  # INV-2 refusal contract intact
        assert a["clarify"] == []
    finally:
        client.__exit__(None, None, None)


def test_answer_round_trips_audit_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("A fasting study [PSG_020503, p.3].")
    )
    client: TestClient = session_client(create_user())
    try:
        r = client.post("/query", json={"question": "What study design is recommended?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] in {"answer", "summary"}
        assert not body["refused"]
        session_id = body["session_id"]
        audit_id = body["audit_id"]
        assert isinstance(audit_id, int)

        with session_scope() as s:
            persisted = [
                (m.role, m.audit_id)
                for m in s.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(col(ChatMessage.created_at).asc())
                )
            ]
        assistant_rows = [m for m in persisted if m[0] == "assistant"]
        assert assistant_rows
        assert assistant_rows[-1][1] == audit_id
    finally:
        client.__exit__(None, None, None)
