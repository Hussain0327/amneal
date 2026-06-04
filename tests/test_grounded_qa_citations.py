"""INV-1: fabricated citation markers must be stripped from the rendered answer.

The citation validator already filters `result.citations` down to verified
citations, but the prose returned to the API/UI must also not contain any
unverifiable `[short_name, p.N]` marker. This test pins that behavior.
"""

from __future__ import annotations

from typing import Any

import pytest
from config.settings import get_settings  # noqa: F401  (mirrors test_invariants imports)

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db
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
    """A real marker is preserved; a fabricated one is removed from the prose."""
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
            ("Dissolution: USP Apparatus 2 at 50 rpm.", _meta(1, 4, "PSG_020503")),
        ]
    )
    answer_text = (
        "A fasting study is recommended [PSG_020503, p.3]. "
        "A fed study is also advised [PSG_FAKE, p.9]."
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(answer_text))

    result = qa_mod.ask("What study design is recommended?")

    assert not result.refused
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020503", 3)}
    assert "[PSG_FAKE, p.9]" not in result.answer
    assert "[PSG_020503, p.3]" in result.answer
