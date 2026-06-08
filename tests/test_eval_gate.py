"""Deterministic eval gate — fires in CI (unlike run_eval, which no-ops on an
empty Chroma).

Seeds a tiny fixed corpus into the isolated per-test store with the echo
embedder, drives the real `grounded_qa.ask()` pipeline (resolve → filter →
retrieve → rerank → cite → validate → answer/refuse), and grades it with the
real `eval.metrics.evaluate`. The only stand-in is a FAITHFUL LLM stub that
reconstructs one citation-terminated sentence per passage it is handed — so the
gate exercises everything except the model, with no network and no API key.

What this protects: product resolution, the mandatory normalized_name filter,
citation validation, the refusal routing, AND now answer CONTENT (fact_recall)
and per-sentence grounding (faithfulness) — all hard-gated at threshold here.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from config.settings import get_settings

from regwatch.eval.metrics import GoldItem, evaluate
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db
from regwatch.store.vector_store import add_chunks

pytestmark = pytest.mark.invariants

# (period-free body text carrying the expected facts, appl_no, normalized_name, page).
# Period-free is load-bearing: the faithful stub emits one sentence per passage
# ending in its citation, and faithfulness() splits on sentence punctuation — an
# internal "." would create an uncited fragment.
_ROWS = [
    (
        "fasting single-dose two-way crossover in vivo bioequivalence study",
        "020503",
        "albuterol sulfate",
        4,
    ),
    (
        "fasting single-dose two-way crossover in vivo study is recommended",
        "020503",
        "albuterol sulfate",
        5,
    ),
    (
        "single actuation content sac fasting two-way crossover in vivo",
        "020911",
        "beclomethasone dipropionate",
        1,
    ),
    (
        "single actuation content sac with acceptance criteria",
        "020911",
        "beclomethasone dipropionate",
        5,
    ),
    (
        "dissolution profile testing across the b lifestage acceptance criteria",
        "214070",
        "albuterol sulfate; budesonide",
        3,
    ),
]

# Self-contained gold set (decoupled from production gold_set.jsonl). The
# expected_sources pages exist in _ROWS; the single-albuterol and combo products
# are cross-product distractors for each other, so the normalized_name filter is
# observably load-bearing (recall stays 1.0 only because it excludes the other).
_GOLD = [
    GoldItem(
        question="What study design does the albuterol sulfate inhalation aerosol PSG recommend?",
        expected_sources=[
            {"short_name": "PSG_020503", "page": 4},
            {"short_name": "PSG_020503", "page": 5},
        ],
        expected_facts=["fasting", "single-dose", "two-way crossover", "in vivo"],
        must_refuse=False,
    ),
    GoldItem(
        question=(
            "What type of study does the beclomethasone dipropionate inhalation aerosol "
            "PSG recommend?"
        ),
        expected_sources=[
            {"short_name": "PSG_020911", "page": 1},
            {"short_name": "PSG_020911", "page": 5},
        ],
        expected_facts=["single actuation content", "SAC"],
        must_refuse=False,
    ),
    GoldItem(
        question="What does the albuterol sulfate and budesonide PSG say about dissolution?",
        expected_sources=[{"short_name": "PSG_214070", "page": 3}],
        expected_facts=["dissolution", "B lifestage"],
        must_refuse=False,
    ),
    # Must-refuse — each refuses via the RESOLVER path (no product resolves → no
    # suggestion → refuse), independent of embedding score.
    GoldItem(
        question="What study type is recommended in the romidepsin PSG?",
        expected_sources=[],
        must_refuse=True,
    ),
    GoldItem(
        question="What submission strategy should we use to file the ANDA?",
        expected_sources=[],
        must_refuse=True,
    ),
    GoldItem(
        question="What is the recommended dose of phantomamole for which no FDA guidance exists?",
        expected_sources=[],
        must_refuse=True,
    ),
    GoldItem(
        question="What does the FDA require for hypotheticol XYZ-9999?",
        expected_sources=[],
        must_refuse=True,
    ),
    GoldItem(
        question="Should we run a fed or fasting study based on internal benchmarks?",
        expected_sources=[],
        must_refuse=True,
    ),
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


_HEADER_RE = re.compile(r"\[([^,\]]+),\s*p\.(\d+)\]")


class _FaithfulStub:
    """Stands in for the synthesizer. Reads the passages it was handed and emits
    one period-free, citation-terminated sentence per passage — faithful by
    construction (cites only what it was given), so every citation validates and
    every sentence carries a citation."""

    name = "faithful-stub"

    def complete(self, messages: list[Any], **_kw: object) -> LLMResponse:
        user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        region = user.split("Source passages:\n", 1)[-1]
        region = region.split("\n\nAnswer with citations", 1)[0]
        sentences: list[str] = []
        for block in (b.strip() for b in region.split("\n---\n") if b.strip()):
            head, _, body = block.partition("\n")
            m = _HEADER_RE.search(head)
            if not m:
                continue
            short, page = m.group(1), m.group(2)
            sentences.append(f"{body.strip()} [{short}, p.{page}].")
        text = " ".join(sentences) if sentences else get_settings().refusal_text
        return LLMResponse(text=text, model=self.name)


def test_eval_gate_passes_on_deterministic_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    # Echo embeddings are hash-based; pin the pre-LLM score floor to 0 so a real
    # item never spuriously refuses. Must-refuse items refuse via the resolver,
    # before retrieval, so this does not weaken them.
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _FaithfulStub())
    _seed()

    sc = evaluate(_GOLD, ask_callable=lambda q: qa_mod.ask(q))

    assert sc.recall_at_k >= 0.90, sc.details
    assert sc.citation_precision >= 0.95, sc.details
    assert sc.refusal_accuracy >= 0.95, sc.details
    # The stub + seed are constructed so these are exactly 1.0 — assert it, so any
    # regression in grounding or fact coverage trips the gate.
    assert sc.faithfulness == 1.0, sc.details
    assert sc.fact_recall == 1.0, sc.details
