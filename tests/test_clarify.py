"""Clarify-over-refuse behavior.

When the system knows *which drug* (or a near-typo of it) but the question is
vague or ambiguous, it should GUIDE the user with options instead of refusing —
without ever fabricating (zero citations) or guessing the drug. Genuine
"not in corpus" cases (e.g. romidepsin, a deliberate must-refuse) still refuse.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.resolver import ExternalDrugMatch
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
    """A bare drug keeps deterministic options and gets an AI guidance turn."""
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


def test_ambiguous_candidates_are_not_sent_as_trusted_product_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router may rank candidates, but none is trusted until the user chooses."""
    _seed(_TWO)
    context: dict[str, Any] = {}

    def _factory(*a: object, **k: object) -> Any:
        class _Planner:
            name = "router-stub"

            def complete(self, messages: list[Any], **kw: object) -> LLMResponse:
                user_message = next(
                    message.content for message in messages if message.role == "user"
                )
                context.update(json.loads(user_message))
                return LLMResponse(
                    text='{"next_step":"choose_product","option_ids":[]}',
                    model="router-stub",
                )

        return _Planner()

    monkeypatch.setattr(qa_mod, "get_llm_provider", _factory)
    result = qa_mod.ask("compare propranolol and metformin studies")

    assert result.status == "clarify"
    assert context["trusted_product_context"] is None


def test_typo_offers_did_you_mean() -> None:
    """A genuine typo (>=88) offers a 'did you mean', and it ASKS (no auto-answer)."""
    _seed(_TWO)
    r = qa_mod.ask("what is the be study for propranlol")
    assert r.status == "clarify"
    assert any(
        o.filters and o.filters.get("normalized_name") == "propranolol hydrochloride"
        for o in r.clarify
    )


def test_romidepsin_never_points_at_a_different_drug() -> None:
    """A drug absent from the corpus must never be answered from another one.

    The OUTCOME changed deliberately (audit #1715): an unresolved product is no
    longer a red "Evidence gap" refusal, because that was indistinguishable
    from a greeting. It is a conversational clarify now. What must not change is
    the safety property -- no answer, no citations, and no silent substitution
    of a drug the user did not ask about (INV-2).
    """
    _seed(_TWO)
    r = qa_mod.ask("What bioequivalence study design is recommended for romidepsin?")
    assert r.refused is False
    assert r.status == "clarify"
    # Without a configured openFDA key there is no positive evidence that
    # romidepsin is a real drug, so it must fall to need_product rather than
    # claim the corpus does not cover it.
    assert r.reason == "need_product"
    assert r.citations == []
    # The substitution guard: neither seeded product may be offered as if it
    # answered the question.
    assert all(seeded not in r.answer.lower() for seeded in _TWO)


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
    monkeypatch.setattr(
        qa_mod,
        "lookup_external_drug",
        lambda *a, **k: ExternalDrugMatch(corpus_products=["amphetamine"], known_absent=False),
    )
    r = qa_mod.ask("adderall")
    assert r.status == "clarify"
    assert not r.refused
    assert any(o.filters and o.filters.get("normalized_name") == "amphetamine" for o in r.clarify)
