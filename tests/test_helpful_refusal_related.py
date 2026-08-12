"""#1 Helpful refusal: a decline carries inert ``related`` pointers.

These pin the additive ``QAResult.related`` field WITHOUT weakening the refusal
contract (INV-2): at a refusal ``refused`` stays True and ``citations`` stays [].
``related`` only adds re-runnable product pointers (NAME + source link), never
the sub-threshold passage text/score. The constrained guidance planner is called
once, but it receives only the trusted route and application-authored options.
"""

from __future__ import annotations

from typing import Any

import pytest
from config.settings import get_settings

from regwatch.api.main import ClarifyOptionOut, QueryResponse
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.retriever import RetrievedPassage
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks
from tests.conftest import synth_turn_json

pytestmark = pytest.mark.invariants


# ---------- Helpers (mirror tests/test_invariants.py) ----------


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


def _passage(
    name: str,
    score: float,
    url: str,
    chunk: str = "c",
    *,
    text: str = "SECRET sub-threshold passage text that must never leak.",
    short_name: str = "PSG_000001",
) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=chunk,
        text=text,
        score=score,
        doc_id=1,
        version_id=10,
        page=2,
        section_path="II.A",
        normalized_name=name,
        source_url=url,
        short_name=short_name,
        metadata={"dosage_form": "Tablet", "route": "Oral"},
    )


def _counting_llm(counter: dict[str, int]) -> Any:
    """A provider factory that records the single constrained guidance call."""

    def _factory(*a: object, **k: object) -> Any:
        counter["n"] += 1

        class _LLM:
            name = "stub"

            def complete(self, *a: object, **kw: object) -> LLMResponse:
                return LLMResponse(text="This should never run.", model="stub")

        return _LLM()

    return _factory


# ---------- #1 low_top_score: weak retrieval declines + offers related ----------


def test_low_top_score_refuses_with_related_and_guidance_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below-threshold retrieval stays uncited while guidance gets one AI turn."""
    init_db()
    threshold = get_settings().refusal_score_threshold
    # Two passages well below threshold; two distinct products + a dup of the
    # first to prove dedup-by-name keeps the first occurrence only.
    weak = [
        _passage("metformin hydrochloride", threshold - 0.2, "http://example/met.pdf", "a"),
        _passage("metformin hydrochloride", threshold - 0.25, "http://example/met2.pdf", "b"),
        _passage("propranolol hydrochloride", threshold - 0.21, "http://example/pro.pdf", "c"),
    ]
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: list(weak))
    counter = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(counter))

    # Pin the product so resolution doesn't short-circuit before retrieval.
    result = qa_mod.ask(
        "What is the BE acceptance interval?",
        filters={"normalized_name": "metformin hydrochloride"},
    )

    # Refusal contract is UNCHANGED.
    assert result.refused is True
    assert result.status == "refused"
    assert result.reason == "low_top_score"
    assert result.citations == []
    # One constrained guidance call runs, without granting answer authority.
    assert counter["n"] == 1
    # related is populated, deduped by product name, NAME only.
    assert result.related, "expected related pointers on a low_top_score refusal"
    names = [o.label for o in result.related]
    assert names == ["Metformin Hydrochloride", "Propranolol Hydrochloride"]  # dedup + order
    for o in result.related:
        assert o.query  # a re-runnable query (the product name)
        # Filters carry retrieval constraints only -- no display values
        # (source_url) in the constraint channel.
        assert o.filters == {"normalized_name": o.query}
        # The sub-threshold passage TEXT never leaks into any related field.
        assert "SECRET" not in o.label
        assert "SECRET" not in o.query
        assert "SECRET" not in str(o.filters)


def test_low_top_score_empty_retrieval_related_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No passages => safe refusal plus one constrained guidance attempt."""
    init_db()
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: [])
    counter = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(counter))
    result = qa_mod.ask(
        "What is the BE acceptance interval?",
        filters={"normalized_name": "metformin hydrochloride"},
    )
    assert result.refused is True
    assert result.citations == []
    assert result.related == []
    assert counter["n"] == 1


def test_low_top_score_guidance_uses_router_without_weak_passage_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router may interpret the question, but weak evidence stays out of its prompt."""
    init_db()
    threshold = get_settings().refusal_score_threshold
    weak = [_passage("metformin hydrochloride", threshold - 0.2, "http://example/met.pdf")]
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: list(weak))
    roles: list[str | None] = []
    captured: list[str] = []

    def _factory(*a: object, **k: object) -> Any:
        roles.append(str(k.get("role")) if k.get("role") is not None else None)

        class _Planner:
            name = "router-stub"

            def complete(self, messages: list[Any], **kw: object) -> LLMResponse:
                captured.extend(message.content for message in messages)
                return LLMResponse(
                    text=('{"next_step":"narrow_source_topic",' '"option_ids":["related:0"]}'),
                    model="router-stub",
                )

        return _Planner()

    monkeypatch.setattr(qa_mod, "get_llm_provider", _factory)
    result = qa_mod.ask(
        "Can you explain the endpoint comparison for this product?",
        filters={"normalized_name": "metformin hydrochloride"},
    )

    assert roles == ["router"]
    assert "SECRET sub-threshold passage" not in "\n".join(captured)
    assert result.status == "refused"
    assert result.reason == "low_top_score"
    assert result.citations == []
    assert result.related[0].label == "Metformin Hydrochloride"
    assert "narrow" in result.answer.lower()


def test_one_strong_hit_cannot_launder_a_weak_neighbor_into_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every passage must clear the threshold before it enters prompt/citation scope."""
    init_db()
    threshold = get_settings().refusal_score_threshold
    retrieved = [
        _passage(
            "metformin hydrochloride",
            threshold + 0.1,
            "http://example/strong.pdf",
            "strong",
            text="A strong, relevant passage about study design.",
            short_name="PSG_STRONG",
        ),
        _passage(
            "metformin hydrochloride",
            threshold - 0.2,
            "http://example/weak.pdf",
            "weak",
            short_name="PSG_WEAK",
        ),
    ]
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: list(retrieved))
    captured: list[str] = []
    roles: list[str | None] = []

    def _factory(*a: object, **k: object) -> Any:
        roles.append(str(k.get("role")) if k.get("role") is not None else None)

        class _Synthesizer:
            name = "synth-stub"

            def complete(self, messages: list[Any], **kw: object) -> LLMResponse:
                captured.extend(message.content for message in messages)
                return LLMResponse(
                    text=synth_turn_json(
                        [("The weak passage supposedly answers it.", [("PSG_WEAK", 2)])]
                    ),
                    model="synth-stub",
                )

        return _Synthesizer()

    monkeypatch.setattr(qa_mod, "get_llm_provider", _factory)
    result = qa_mod.ask(
        "What study design is recommended?",
        filters={"normalized_name": "metformin hydrochloride"},
    )

    assert roles == ["synthesizer"]
    assert "SECRET sub-threshold passage" not in "\n".join(captured)
    assert result.refused is True
    assert result.reason == "no_valid_citations"
    assert result.citations == []
    assert {row["chunk_id"] for row in result.retrieved} == {"strong", "weak"}


def test_guidance_provider_failure_is_attributed_to_router_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed router attempt must not be audited as the synthesizer model."""
    init_db()
    roles: list[str | None] = []
    monkeypatch.setattr(
        qa_mod,
        "current_model_name",
        lambda *, role: f"configured-{role}",
    )

    def _boom(*a: object, **k: object) -> Any:
        roles.append(str(k.get("role")) if k.get("role") is not None else None)
        raise RuntimeError("router unavailable")

    monkeypatch.setattr(qa_mod, "get_llm_provider", _boom)
    result = qa_mod.ask("What study is recommended for romidepsin?")

    assert roles == ["router"]
    assert result.model_name == "configured-router"
    with session_scope() as session:
        row = session.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.model_name == "configured-router"
        assert row.route_json["guidance"]["fallback_reason"] == "provider_error"


# ---------- #1 no_product: typo => related from suggest_products ----------


def test_no_product_typo_populates_related_from_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo gets trusted suggestions prioritized by one guidance call."""
    _seed(["propranolol hydrochloride", "metformin hydrochloride"])
    counter = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(counter))

    result = qa_mod.ask("what is the be study for propranlol")

    assert counter["n"] == 1
    assert result.related, "expected related pointers from suggest_products"
    assert any(
        o.filters and o.filters.get("normalized_name") == "propranolol hydrochloride"
        for o in result.related
    )


# ---------- #1 no_product: absent drug => related == [], graceful ----------


def test_no_product_absent_drug_related_empty_no_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely-absent drug clarifies with no options and no exception.

    The empty-resolver path must not crash. Since audit #1715 the outcome is a
    conversational clarify rather than a refusal, and with no options to order
    the bounded guidance planner is skipped entirely -- a greeting-shaped turn
    must not pay a model round trip whose text is discarded anyway.
    """
    _seed(["propranolol hydrochloride", "metformin hydrochloride"])
    counter = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(counter))

    result = qa_mod.ask("What bioequivalence study design is recommended for romidepsin?")

    assert result.refused is False
    assert result.status == "clarify"
    assert result.reason == "need_product"
    assert result.citations == []
    assert result.related == []
    assert result.clarify == []
    assert counter["n"] == 0


# ---------- contract: QueryResponse round-trips related + reason ----------


def test_query_response_round_trips_related_and_reason() -> None:
    """The wire model carries `related` (clarify-option shape) and `reason`."""
    resp = QueryResponse(
        answer="I can't answer that from the FDA sources I have.",
        citations=[],
        refused=True,
        model_name="stub",
        audit_id=1,
        session_id="s",
        turn_id="t",
        status="refused",
        reason="low_top_score",
        related=[
            ClarifyOptionOut(
                label="Metformin Hydrochloride",
                query="metformin hydrochloride",
                filters={"normalized_name": "metformin hydrochloride", "source_url": "http://x"},
            )
        ],
    )
    dumped = resp.model_dump()
    assert dumped["reason"] == "low_top_score"
    assert dumped["related"][0]["label"] == "Metformin Hydrochloride"
    assert dumped["related"][0]["filters"]["source_url"] == "http://x"
    # Re-validate from the dumped dict (full round-trip).
    again = QueryResponse.model_validate(dumped)
    assert again.reason == "low_top_score"
    assert again.related[0].query == "metformin hydrochloride"
    # The refusal contract: related is additive, citations stay empty.
    assert again.refused is True
    assert again.citations == []
