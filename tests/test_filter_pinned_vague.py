"""Greeting / no-topic inputs with a filter-pinned product.

The UI's "Active ingredient" field pins ``normalized_name`` via filters, which
skips question-side product resolution. The vague/no-topic guard must still
fire on that path: a greeting must CLARIFY (zero citations, one audit row),
never reach the synthesizer and come back as a cited greeting — while a real
question with the same pinned filter still answers.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks

_TWO = ["propranolol hydrochloride", "metformin hydrochloride"]
_PINNED = {"normalized_name": "propranolol hydrochloride"}


def _seed(names: list[str]) -> None:
    """Seed the vector store with one chunk per drug name."""
    init_db()
    embedder = get_embedding_provider()
    texts = [f"Bioequivalence study guidance for {n}." for n in names]
    embeddings = embedder.embed(texts)
    metas = [
        {
            "doc_id": i + 1,
            "version_id": (i + 1) * 10,
            "page": 1,
            "section_path": "II.A",
            "normalized_name": n,
            "dosage_form": "Tablet",
            "route": "Oral",
            "source_url": f"http://example/{i}.pdf",
            "psg_type": "draft",
            "appl_no": f"0{i}001",
        }
        for i, n in enumerate(names)
    ]
    add_chunks(
        ids=[f"chunk-{i}" for i in range(len(names))],
        embeddings=embeddings,
        documents=texts,
        metadatas=metas,
    )


def _query_logs() -> list[tuple[str, list[object]]]:
    with session_scope() as s:
        return [(str(row.status), list(row.citations_json)) for row in s.scalars(select(QueryLog))]


def _row_count() -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(QueryLog)) or 0)


def test_greeting_with_filter_pinned_clarifies() -> None:
    """'Hello' + Active-ingredient pin → clarify with options, never a cited greeting."""
    _seed(_TWO)
    r = qa_mod.ask("Hello", filters=dict(_PINNED))
    assert r.status == "clarify"
    assert not r.refused
    assert not r.citations  # never fabricates / never cites a greeting
    assert r.clarify  # offered options
    assert all(
        o.filters and o.filters.get("normalized_name") == "propranolol hydrochloride"
        for o in r.clarify
    )
    assert "propranolol" in (r.interpretation or "").lower()


def test_thanks_with_filter_pinned_clarifies() -> None:
    """A no-topic 'thanks' variant clarifies too — nothing reaches the synthesizer."""
    _seed(_TWO)
    r = qa_mod.ask("thanks!", filters=dict(_PINNED))
    assert r.status == "clarify"
    assert not r.refused
    assert not r.citations
    assert r.clarify


def test_noncanonical_pin_clarifies_with_canonical_options() -> None:
    """A title-cased UI pin is canonicalized before building the clarify options."""
    _seed(_TWO)
    r = qa_mod.ask("hi", filters={"normalized_name": "Propranolol Hydrochloride"})
    assert r.status == "clarify"
    assert all(
        o.filters and o.filters.get("normalized_name") == "propranolol hydrochloride"
        for o in r.clarify
    )


def test_filter_pinned_greeting_logs_exactly_one_audit_row() -> None:
    """INV-6: the filter-pinned clarify path audits exactly once, citation-free."""
    _seed(_TWO)
    assert _row_count() == 0
    qa_mod.ask("Hello", filters=dict(_PINNED))
    logs = _query_logs()
    assert logs == [("clarify", [])]


def test_real_question_with_filter_pinned_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: a topical question with the same pin must still answer (not clarify)."""
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")  # isolate from echo-embedding score
    import config.settings as cs

    cs.get_settings.cache_clear()
    _seed(_TWO)
    r = qa_mod.ask("What dissolution testing is required?", filters=dict(_PINNED))
    assert r.status == "answer"
    assert not r.refused
    assert r.citations
    assert all(c.short_name == "PSG_00001" for c in r.citations)
