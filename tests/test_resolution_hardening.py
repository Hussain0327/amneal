"""Entity/source-resolution hardening (#2).

Three guarantees:
  - product/document keys: a normalized_name filter in any casing/salt-order is
    canonicalized so retrieval's exact-match filter cannot silently miss;
  - AND validation: an explicit comparison naming 2+ products CLARIFIES, even
    when one product's ingredients are a subset of another's;
  - clarify over unclear rows: evidence spanning >1 product CLARIFIES (offers the
    products), it does not bluntly refuse.
"""

from __future__ import annotations

import pytest
from config.settings import get_settings
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.resolver import resolve_product
from regwatch.retrieve.retriever import RetrievedPassage
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks

CORPUS = {
    "albuterol sulfate",
    "albuterol sulfate; budesonide",
    "beclomethasone dipropionate",
    "levalbuterol tartrate",
}


# ---------- 2b. AND validation ----------
def test_comparison_clarifies_over_subset_collapse() -> None:
    # "compare <combo> with <single>" — the single's ingredient set is a subset of
    # the combo's, so the default subset-collapse would resolve to the combo. The
    # explicit comparison must clarify instead.
    r = resolve_product(
        "compare albuterol sulfate and budesonide with albuterol sulfate", products=CORPUS
    )
    assert r.status == "ambiguous"
    assert "albuterol sulfate" in r.candidates
    assert "albuterol sulfate; budesonide" in r.candidates


def test_versus_marker_clarifies() -> None:
    r = resolve_product("albuterol sulfate vs beclomethasone dipropionate", products=CORPUS)
    assert r.status == "ambiguous"
    assert set(r.candidates) >= {"albuterol sulfate", "beclomethasone dipropionate"}


def test_plain_combo_question_still_resolves_to_combo() -> None:
    # No comparison marker ("and" is NOT a marker) → combo wins, unchanged.
    r = resolve_product("What does the albuterol sulfate and budesonide PSG say?", products=CORPUS)
    assert r.status == "resolved"
    assert r.normalized_name == "albuterol sulfate; budesonide"


# ---------- 2a. product/document keys ----------
def _seed_one(name: str, appl: str) -> None:
    init_db()
    emb = get_embedding_provider()
    text = f"Bioequivalence study guidance for {name} with fasting two-way crossover."
    add_chunks(
        ids=[f"{appl}-1"],
        embeddings=emb.embed([text]),
        documents=[text],
        metadatas=[
            {
                "doc_id": 1,
                "version_id": 10,
                "page": 1,
                "normalized_name": name,
                "appl_no": appl,
                "source_url": f"http://example/PSG_{appl}.pdf",
                "section_path": "",
                "dosage_form": "Aerosol, Metered",
                "route": "Inhalation",
                "psg_type": "draft",
            }
        ],
    )


def test_noncanonical_filter_is_canonicalized(monkeypatch: pytest.MonkeyPatch) -> None:
    # A title-case filter from the API must match the stored canonical key
    # ("albuterol sulfate"), not silently miss the exact-match Chroma filter.
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")  # isolate from echo-embedding score
    import config.settings as cs

    cs.get_settings.cache_clear()
    _seed_one("albuterol sulfate", "020503")
    r = qa_mod.ask(
        "What study design does the PSG recommend?",
        filters={"normalized_name": "Albuterol Sulfate"},
    )
    assert not r.refused
    assert r.status == "answer"
    assert any(c.short_name == "PSG_020503" for c in r.citations)


# ---------- 2c. clarify over unclear rows ----------
def _passage(name: str, appl: str, page: int, score: float = 0.9) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=f"{appl}-{page}",
        text=f"Study guidance text for {name}.",
        score=score,
        doc_id=1,
        version_id=10,
        page=page,
        section_path=None,
        normalized_name=name,
        source_url=f"http://example/PSG_{appl}.pdf",
        short_name=f"PSG_{appl}",
        metadata={},
    )


def _query_log_count() -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(QueryLog)) or 0)


def test_mixed_product_evidence_clarifies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate evidence that spans two products (a caller that bypassed the
    # resolver / a filter that failed to constrain). The pipeline must clarify,
    # offering each product, never cite across them or bluntly refuse.
    init_db()
    mixed = [
        _passage("albuterol sulfate", "020503", 4),
        _passage("beclomethasone dipropionate", "020911", 1),
    ]
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: mixed)

    before = _query_log_count()
    r = qa_mod.ask(
        "What study design does the PSG recommend?",
        filters={"normalized_name": "albuterol sulfate"},
    )

    assert r.status == "clarify"
    assert not r.refused
    assert not r.citations  # never fabricates across products
    assert {o.filters["normalized_name"] for o in r.clarify if o.filters} == {
        "albuterol sulfate",
        "beclomethasone dipropionate",
    }
    assert _query_log_count() == before + 1  # INV-6: audits exactly once
    assert r.answer != get_settings().refusal_text
