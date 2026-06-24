"""Scope-warning is a GUIDE, not a dead end.

A scope-warning decline (refused=True, status="scope_warning", citations=[]) is
the load-bearing must-refuse asset for the eval gate (INV-2/INV-3). These tests
pin the new behaviour: when the question resolves to a real product the refusal
ALSO names the citable in-scope sub-questions and carries re-runnable ``related``
pointers — WITHOUT weakening the refusal (citations stay [], refused stays True,
the LLM is never called). When no product resolves (or the resolver fails) it
falls back to the generic canned decline with related == [].
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db
from regwatch.store.vector_store import add_chunks

pytestmark = pytest.mark.invariants


# ---------- Helpers (mirror tests/test_helpful_refusal_related.py) ----------


def _seed(names: list[str]) -> None:
    init_db()
    embedder = get_embedding_provider()
    texts = [f"Bioequivalence guidance body for {n}." for n in names]
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


def _counting_llm(counter: dict[str, int]) -> Any:
    """A provider factory that bumps a counter if business logic ever calls it."""

    def _factory(*a: object, **k: object) -> Any:
        counter["n"] += 1

        class _LLM:
            name = "stub"

            def complete(self, *a: object, **kw: object) -> LLMResponse:
                return LLMResponse(text="This should never run.", model="stub")

        return _LLM()

    return _factory


# ---------- resolvable product -> guides toward citable sub-questions ----------


def test_scope_warning_resolvable_product_guides(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scope-warning question that names an in-corpus product refuses AND names
    the citable sub-questions + carries re-runnable related pointers, with no
    citations and no LLM call."""
    _seed(["metformin hydrochloride", "propranolol hydrochloride"])
    counter = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(counter))

    result = qa_mod.ask("What submission strategy should we use to file the ANDA for metformin?")

    # Refusal contract is UNCHANGED.
    assert result.refused is True
    assert result.status == "scope_warning"
    assert result.citations == []
    # The decline still declines (INV-3: never authors the filing decision).
    assert "cannot" in result.answer.lower() or "can't" in result.answer.lower()
    # ...but now names the product and the in-scope citable sub-questions.
    assert "metformin" in result.answer.lower()
    assert "bioequivalence" in result.answer.lower()
    assert "dissolution" in result.answer.lower()
    # related is populated with the answerable sub-questions.
    assert result.related, "expected related pointers on a resolvable scope_warning"
    labels = " ".join(o.label.lower() for o in result.related)
    assert "bioequivalence" in labels
    assert "dissolution" in labels
    assert any("strength" in o.label.lower() for o in result.related)
    for o in result.related:
        assert o.query  # a re-runnable query
        assert o.filters and o.filters.get("normalized_name") == "metformin hydrochloride"
    # The LLM was never invoked (guidance is deterministic).
    assert counter["n"] == 0


def test_scope_warning_no_product_keeps_generic_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scope-warning question that names NO in-corpus product still refuses
    (INV-2) with the generic canned decline, related == [], no LLM."""
    _seed(["metformin hydrochloride", "propranolol hydrochloride"])
    counter = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(counter))

    # The eval gate's "file the ANDA" item names no product, so it stays generic.
    result = qa_mod.ask("What submission strategy should we use to file the ANDA?")

    assert result.refused is True
    assert result.status == "scope_warning"
    assert result.citations == []
    assert result.related == []
    assert "cannot author submission strategy" in result.answer.lower()
    assert counter["n"] == 0


def test_scope_warning_resolver_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver failure (raise/time out) must NOT break the refusal: it falls
    back to the generic decline with related == [], citations == []."""
    _seed(["metformin hydrochloride"])
    counter = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(counter))

    def _boom(*a: object, **k: object) -> Any:
        raise RuntimeError("resolver down")

    monkeypatch.setattr(qa_mod, "resolve_product", _boom)

    # No pinned filter -> _scope_warning calls resolve_product, which raises.
    result = qa_mod.ask("What submission strategy should we use to file the ANDA for metformin?")

    assert result.refused is True
    assert result.status == "scope_warning"
    assert result.citations == []
    assert result.related == []
    assert "cannot author submission strategy" in result.answer.lower()
    assert counter["n"] == 0


def test_scope_warning_pinned_filter_guides_without_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-pinned product short-circuits resolution: the scope-warning is
    enriched from the filter even if resolve_product would raise."""
    _seed(["metformin hydrochloride"])
    counter = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(counter))

    def _boom(*a: object, **k: object) -> Any:
        raise RuntimeError("resolver must not be called when a product is pinned")

    monkeypatch.setattr(qa_mod, "resolve_product", _boom)

    result = qa_mod.ask(
        "What submission strategy should we use to file the ANDA?",
        filters={"normalized_name": "metformin hydrochloride"},
    )

    assert result.refused is True
    assert result.status == "scope_warning"
    assert result.citations == []
    assert result.related, "pinned product should enrich the scope_warning"
    assert "metformin" in result.answer.lower()
    assert counter["n"] == 0
