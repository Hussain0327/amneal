"""Deterministic eval gate — fires in CI (unlike run_eval, which no-ops on an
empty Chroma).

Seeds a tiny fixed corpus into the isolated per-test store with the echo
embedder, drives the real `grounded_qa.ask()` pipeline (resolve -> filter ->
retrieve -> rerank -> admit claims -> render/refuse), and grades it with the real
`eval.metrics.evaluate`. The only stand-in is a FAITHFUL LLM stub that returns
the STRUCTURED turn the real synthesizer now returns: one claim per passage it
was handed, each declaring that passage's own (short_name, page). So the gate
exercises everything except the model, with no network and no API key.

What this protects: product resolution, the mandatory normalized_name filter,
claim admission against the passages actually retrieved, the refusal routing,
AND answer CONTENT (fact_recall) and per-sentence grounding (faithfulness) --
all hard-gated at threshold here. faithfulness stays a real assertion because
the RENDERER, not the model, writes every marker and places it before the
terminal punctuation the scorer splits on.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from regwatch.eval.metrics import GoldItem, evaluate
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.store.vector_store import add_chunks
from tests.conftest import synth_turn_json

pytestmark = pytest.mark.invariants

# (period-free body text carrying the expected facts, appl_no, normalized_name, page).
# Period-free is still load-bearing, for a NEW reason: the stub puts one passage
# body into one claim slot, and the turn gate drops any claim whose sanitized
# text is more than one sentence (a slot must not hold a cited fact plus an
# uncited rider). An internal "." would therefore drop the claim, not merely
# create an uncited fragment.
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
    """Stands in for the STRUCTURED synthesizer.

    Reads the passages it was handed and returns one JSON turn carrying one
    claim per passage: the passage body as the claim text, that passage's own
    (short_name, page) as its declared citation. Faithful by construction -- it
    declares only what it was given -- so every claim is admitted, the renderer
    stamps every marker, and no claim is ever dropped. It authors NO markers of
    its own; that is now the renderer's job alone.
    """

    name = "faithful-stub"

    def complete(self, messages: list[Any], **_kw: object) -> LLMResponse:
        user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        region = user.split("<untrusted_source_passages>\n", 1)[-1]
        region = region.split("\n</untrusted_source_passages>", 1)[0]
        claims: list[tuple[str, list[tuple[str, int]]]] = []
        for block in (b.strip() for b in region.split("\n---\n") if b.strip()):
            head, _, body = block.partition("\n")
            m = _HEADER_RE.search(head)
            if not m:
                continue
            claims.append((body.strip(), [(m.group(1), int(m.group(2)))]))
        if not claims:
            # Nothing to cite -> the model DECLINES, the structured twin of the
            # old refusal-string emission.
            return LLMResponse(text=synth_turn_json(turn_type="NO_EVIDENCE"), model=self.name)
        return LLMResponse(text=synth_turn_json(claims), model=self.name)


def test_eval_gate_passes_on_deterministic_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    # Echo embeddings are hash-based; pin the pre-LLM score floor to 0 so a real
    # item never spuriously refuses. Must-refuse items refuse via the resolver,
    # before retrieval, so this does not weaken them.
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
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


# Synthetic two-form drug — one normalized_name, two distinct (dosage_form, route)
# combos. Seeded into BOTH the SQL catalog (psg_document + psg_version, which the
# pre-retrieval multi-form guard enumerates) and Chroma. A BE-study question must
# CLARIFY which form rather than blend both into one LLM context (the wrong-form
# citation bug). Gated offline forever — no network, no API key.
_TWO_FORM_ROWS = [
    ("syntheticol oral tablet bioequivalence study guidance", "Tablet", "Oral", 1),
    ("syntheticol transdermal patch bioequivalence study guidance", "Patch", "Transdermal", 2),
]


def _seed_two_form_drug() -> None:
    init_db()
    with session_scope() as s:
        for idx, (_, form, route, _page) in enumerate(_TWO_FORM_ROWS):
            doc = PsgDocument(
                active_ingredient="Syntheticol",
                normalized_name="syntheticol",
                dosage_form=form,
                route=route,
                appl_no=f"99900{idx}",
                psg_type="draft",
                recommended_date="2026-01-01",
                source_url=f"http://example/PSG_99900{idx}.pdf",
                content_hash=f"hash-99900{idx}",
            )
            s.add(doc)
            s.flush()
            assert doc.id is not None
            s.add(PsgVersion(psg_document_id=doc.id, content_hash=f"hash-99900{idx}"))

    emb = get_embedding_provider()
    texts = [t for t, _, _, _ in _TWO_FORM_ROWS]
    add_chunks(
        ids=[f"syntheticol-{i}" for i in range(len(_TWO_FORM_ROWS))],
        embeddings=emb.embed(texts),
        documents=texts,
        metadatas=[
            {
                "doc_id": idx + 1,
                "version_id": idx + 1,
                "page": page,
                "normalized_name": "syntheticol",
                "appl_no": f"99900{idx}",
                "source_url": f"http://example/PSG_99900{idx}.pdf",
                "section_path": "",
                "dosage_form": form,
                "route": route,
                "psg_type": "draft",
            }
            for idx, (_, form, route, page) in enumerate(_TWO_FORM_ROWS)
        ],
    )


def test_eval_gate_clarifies_on_multiform_drug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _FaithfulStub())
    _seed_two_form_drug()

    gold = [
        GoldItem(
            question="What bioequivalence study design does FDA recommend for syntheticol?",
            expected_sources=[],
            must_clarify=True,
        )
    ]
    sc = evaluate(gold, ask_callable=lambda q: qa_mod.ask(q))

    # The must_clarify item folds into refusal_accuracy (decision accuracy); it is
    # correct iff the system clarified rather than blending the two forms.
    assert sc.clarified_correctly == 1, sc.details
    assert sc.refusal_accuracy >= 0.95, sc.details


# White-Paper deterministic gate. A faithful STRUCTURED stub stands in for the
# live FDA sources (Orange Book / Drugs@FDA / NDC / DailyMed / Shortages / REMS)
# and the scoped PSG ask(); the populator's real cell logic runs against it and
# is graded by the real eval.whitepaper_metrics at the SAME thresholds. No
# network, no API key — so this gate fires in CI.
def test_eval_gate_whitepaper_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.eval.whitepaper_metrics import evaluate_whitepaper, load_whitepaper_gold
    from regwatch.whitepaper.populator import build_whitepaper
    from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources

    install_fake_sources(monkeypatch)
    result = build_whitepaper(RLD_NAME, APPL_NO)
    items = load_whitepaper_gold()
    assert len(items) >= 10, "expected at least 10 white-paper gold items"

    sc = evaluate_whitepaper(result, items)
    assert sc.recall_at_k >= 0.90, sc.details
    assert sc.citation_precision >= 0.95, sc.details
    assert sc.refusal_accuracy >= 0.95, sc.details
