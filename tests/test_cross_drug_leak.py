"""INV-9: product resolution before retrieval prevents cross-drug citation leak.

FDA PSG templates reuse identical language across drugs. Here albuterol and
beclomethasone chunks carry the SAME "Single actuation content (SAC)" /
"two-way crossover" boilerplate. A beclomethasone question must:
  - resolve to beclomethasone and filter retrieval to it,
  - never retrieve an albuterol chunk,
  - never let an albuterol citation survive validation.
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

# (text, appl_no, normalized_name, page) — same boilerplate across both drugs.
_BOILERPLATE = "Type of study: Single actuation content (SAC). Fasting, two-way crossover, in vivo."
_ROWS = [
    (_BOILERPLATE, "020503", "albuterol sulfate", 2),
    ("Albuterol strengths and reference details.", "020503", "albuterol sulfate", 1),
    (_BOILERPLATE, "020911", "beclomethasone dipropionate", 2),
    ("Beclomethasone strengths and reference details.", "020911", "beclomethasone dipropionate", 1),
]


def _seed() -> None:
    init_db()
    emb = get_embedding_provider()
    texts = [t for t, _, _, _ in _ROWS]
    vecs = emb.embed(texts)
    ids = [f"{appl}-{page}" for _, appl, _, page in _ROWS]
    metas = [
        {
            "doc_id": idx + 1,
            "version_id": (idx + 1) * 10,
            "page": page,
            "normalized_name": name,
            "appl_no": appl,
            "source_url": f"http://example/PSG_{appl}.pdf",
            "section_path": "",
            "dosage_form": "Aerosol, Metered",
            "route": "Inhalation",
            "psg_type": "draft",
        }
        for idx, (_, appl, name, page) in enumerate(_ROWS)
    ]
    add_chunks(ids=ids, embeddings=vecs, documents=texts, metadatas=metas)


def _stub_llm(text: str) -> Any:
    class _LLM:
        name = "stub"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub")

    return _LLM()


def test_beclomethasone_question_cannot_leak_albuterol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed()
    # Answer cites the right drug AND illegally cites albuterol's boilerplate page.
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")  # ensure we reach the LLM path
    import config.settings as cs

    cs.get_settings.cache_clear()
    leaky = "Single actuation content (SAC) [PSG_020911, p.2]. See also [PSG_020503, p.2]."
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(leaky))

    result = qa_mod.ask("What type of study does the beclomethasone dipropionate PSG recommend?")

    assert not result.refused
    # Retrieval was constrained to beclomethasone — no albuterol chunk surfaced.
    assert {r["short_name"] for r in result.retrieved} == {"PSG_020911"}
    # The leaked albuterol citation did not survive validation...
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020911", 2)}
    # ...and is stripped from the rendered prose.
    assert "PSG_020503" not in result.answer
