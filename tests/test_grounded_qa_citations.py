"""INV-1: fabricated citation markers must be stripped from the rendered answer.

The citation validator already filters `result.citations` down to verified
citations, but the prose returned to the API/UI must also not contain any
unverifiable `[short_name, p.N]` marker. This test pins that behavior.
"""

from __future__ import annotations

from typing import Any

import pytest
from config.settings import get_settings

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.generate.prompts import GROUNDED_QA_PROMPT
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks

pytestmark = pytest.mark.invariants


# ---------- Helpers (mirrors tests/test_invariants.py) ----------


def _seed_corpus(passages_with_meta: list[tuple[str, dict]]) -> None:
    """Seed the test vector store with passages and their metadata."""
    init_db()
    embedder = get_embedding_provider()
    texts = [p for p, _ in passages_with_meta]
    embeddings = embedder.embed(texts)
    ids = [f"chunk-{i}" for i in range(len(texts))]
    metas = [m for _, m in passages_with_meta]
    add_chunks(ids=ids, embeddings=embeddings, documents=texts, metadatas=metas)


def _meta(doc_id: int, page: int, short: str = "PSG_020503") -> dict:
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


def _stub_llm(text: str) -> Any:
    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    return _LLM()


# ---------- INV-1: fabricated markers stripped from prose ----------


def test_inv1_fabricated_citation_stripped_from_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fabricated marker cannot leave its now-uncited claim beside a valid one."""
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
            ("Dissolution: USP Apparatus 2 at 50 rpm.", _meta(1, 4, "PSG_020503")),
        ]
    )
    answer_text = (
        "A fasting study is recommended [PSG_020503, p.3]. "
        "A fed study is also advised [PSG_999999, p.9]."
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(answer_text))

    result = qa_mod.ask("What study design is recommended?")

    assert result.refused
    assert result.citations == []
    assert "[PSG_999999, p.9]" not in result.answer
    assert result.answer == get_settings().refusal_text


def test_mixed_case_duplicate_of_valid_citation_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validator dedupes case-insensitively, but a LATER occurrence of the
    same valid citation in different casing must survive the prose filter --
    stripping it (as if fabricated) would leave its sentence uncited."""
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
        ]
    )
    answer_text = (
        "The BE study is a two-way crossover [PSG_020503, p.3]. "
        "The waiver criteria also apply [psg_020503, p.3]."
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(answer_text))

    result = qa_mod.ask("What study design is recommended?")

    assert not result.refused
    # BOTH occurrences keep their marker, each in its as-emitted casing...
    assert "[PSG_020503, p.3]" in result.answer
    assert "[psg_020503, p.3]" in result.answer
    # ...while the citations list stays deduped to one validated entry.
    assert {(c.short_name.upper(), c.page) for c in result.citations} == {("PSG_020503", 3)}
    assert len(result.citations) == 1


def test_supported_partial_answer_is_accepted_and_prompt_identity_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    answer_text = (
        "A fasting bioequivalence study is recommended [PSG_020503, p.3].\n"
        "Evidence not found in the supplied passages for: waiver conditions."
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(answer_text))

    result = qa_mod.ask(
        "What study design and waiver conditions does the albuterol sulfate guidance state?"
    )

    assert not result.refused
    assert "Evidence not found" in result.answer
    with session_scope() as session:
        row = session.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.route_json["prompt"] == GROUNDED_QA_PROMPT.as_dict()
        assert row.route_json["partial_evidence"] is True


# ---------- citation binds to the best-ranked same-page chunk ----------


def test_validate_citations_binds_best_ranked_same_page_chunk() -> None:
    """When several retrieved chunks share a (doc, page), the validated citation
    must carry the TOP-ranked chunk's id/snippet/score (passages arrive
    best-first) -- not the weakest one that happened to be listed last."""
    from regwatch.retrieve.retriever import RetrievedPassage

    def _passage(chunk_id: str, score: float, text: str) -> RetrievedPassage:
        return RetrievedPassage(
            chunk_id=chunk_id,
            text=text,
            score=score,
            doc_id=1,
            version_id=10,
            page=3,
            section_path=None,
            normalized_name="albuterol sulfate",
            source_url="http://example/PSG_020503.pdf",
            short_name="PSG_020503",
            metadata={},
        )

    best = _passage("chunk-best", 0.71, "Dissolution: USP paddle method at 50 rpm.")
    worse = _passage("chunk-worse", 0.34, "Table 2 footnote.")
    validated, bad = qa_mod._validate_citations(
        "The dissolution method is the USP paddle [PSG_020503, p.3].", [best, worse]
    )

    assert bad == []
    assert [c.chunk_id for c in validated] == ["chunk-best"]
    assert validated[0].score == 0.71
    assert "USP paddle" in validated[0].snippet


# ---------- audit-write failure has a DEFINED shape (never a naked 500) ----------


def test_audit_write_failure_degrades_to_error_refusal_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the QueryLog write fails after a successful synthesis, ask() must
    return the fixed-copy status='error' refusal (audit skipped, flagged) --
    the validated answer is withheld (no-audit-no-answer) but the client gets
    a defined refusal instead of a 500 that re-runs the pipeline."""
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
        ]
    )
    answer_text = "A fasting study is recommended [PSG_020503, p.3]."
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(answer_text))

    def _boom(**kwargs: object) -> int:
        raise RuntimeError("simulated audit db outage")

    monkeypatch.setattr(qa_mod, "log_query", _boom)

    result = qa_mod.ask("What study design is recommended?")

    assert result.refused is True
    assert result.status == "error"
    assert result.citations == []
    assert "temporarily unavailable" in result.answer
    # The paid, validated answer never leaks on the unaudited path (INV-6).
    assert "PSG_020503" not in result.answer
    assert "fasting study" not in result.answer
    # The refusal's own audit write also failed -> skipped, sentinel id.
    assert result.audit_id == -1
