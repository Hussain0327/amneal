"""Clarify-over-refuse behavior.

When the system knows *which drug* (or a near-typo of it) but the question is
vague or ambiguous, it should GUIDE the user with options instead of refusing —
without ever fabricating (zero citations) or guessing the drug. Genuine
"not in corpus" cases (e.g. romidepsin, a deliberate must-refuse) still refuse.
"""

from __future__ import annotations

import pytest
from config.settings import get_settings
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks


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


def _row_count() -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(QueryLog)) or 0)


_TWO = ["propranolol hydrochloride", "metformin hydrochloride"]


def test_bare_drug_name_clarifies() -> None:
    """A bare, name-matched drug → clarify with options, deterministically (no LLM)."""
    _seed(_TWO)
    r = qa_mod.ask("propranolol")
    assert r.status == "clarify"
    assert not r.refused
    assert r.clarify  # offered options
    assert "propranolol" in (r.interpretation or "").lower()
    assert not r.citations  # never fabricates


def test_vague_conversational_phrasing_clarifies() -> None:
    """The hero case: 'i need help on propranolol' guides instead of refusing."""
    _seed(_TWO)
    r = qa_mod.ask("i need help on propranolol")
    assert r.status == "clarify"
    assert not r.refused


def test_two_matched_drugs_clarify_with_candidates() -> None:
    """Ambiguous (2+ products named) → ask which, offering each as an option."""
    _seed(_TWO)
    r = qa_mod.ask("compare propranolol and metformin studies")
    assert r.status == "clarify"
    assert {o.filters["normalized_name"] for o in r.clarify if o.filters} == set(_TWO)


def test_typo_offers_did_you_mean() -> None:
    """A genuine typo (>=88) offers a 'did you mean', and it ASKS (no auto-answer)."""
    _seed(_TWO)
    r = qa_mod.ask("what is the be study for propranlol")
    assert r.status == "clarify"
    assert any(
        o.filters and o.filters.get("normalized_name") == "propranolol hydrochloride"
        for o in r.clarify
    )


def test_romidepsin_stays_refused_with_no_suggestion() -> None:
    """A drug genuinely absent from the corpus must refuse — never silently
    point at a different drug (INV-2 must-refuse asset)."""
    _seed(_TWO)
    r = qa_mod.ask("What bioequivalence study design is recommended for romidepsin?")
    assert r.refused
    assert r.status == "refused"
    assert not r.clarify
    assert r.answer == get_settings().refusal_text


def test_clarify_logs_exactly_one_audit_row() -> None:
    """INV-6: the clarify path audits exactly once, like answer and refuse."""
    _seed(_TWO)
    assert _row_count() == 0
    qa_mod.ask("propranolol")
    assert _row_count() == 1


def test_brand_name_clarifies_via_generic_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A brand name (no in-corpus product, no typo match) → clarify with the
    generic(s) the brand maps to. openFDA is stubbed so the test stays offline."""
    _seed(["amphetamine", "propranolol hydrochloride"])
    monkeypatch.setattr(qa_mod, "resolve_brand", lambda *a, **k: ["amphetamine"])
    r = qa_mod.ask("adderall")
    assert r.status == "clarify"
    assert not r.refused
    assert any(o.filters and o.filters.get("normalized_name") == "amphetamine" for o in r.clarify)
