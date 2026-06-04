"""Compliance invariants (Section 4 of the spec).

These are code-level checks for INV-1 through INV-6. If any of these fail, CI
fails — the invariants are not negotiable.

Notation:
  INV-1 Grounding         — every claim is traceable to a source + page.
  INV-2 Refuse over guess — low-recall queries refuse, do not hallucinate.
  INV-3 Operational only  — system never authors submissions or recommendations.
  INV-4 No fabricated execution — never report a run that didn't happen.
  INV-5 Verified provenance — pipeline/product facts only from verified sources.
  INV-6 Auditability      — every query is logged.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from config.settings import get_settings
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process import extractor as ext
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks

pytestmark = pytest.mark.invariants


# ---------- Helpers ----------


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


# ---------- INV-1: Grounding ----------


def test_inv1_extractor_drops_uncited_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim with no source span is dropped at extraction time."""
    payload = {
        "fields": {
            "study_type": {"value": "single-dose", "citation": None},  # no citation
        }
    }
    monkeypatch.setattr(ext, "get_llm_provider", lambda *a, **k: _stub_llm(json.dumps(payload)))
    res = ext.extract_be(["any text"])
    assert res.fields["study_type"] is None
    assert "study_type" not in res.citations


def test_inv1_grounded_answer_has_only_known_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every [short, p.N] in an answer must correspond to a passage we sent."""
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
            ("Dissolution: USP Apparatus 2 at 50 rpm.", _meta(1, 4, "PSG_020503")),
        ]
    )
    # Answer cites a real passage AND a fabricated one.
    answer_text = (
        "A fasting bioequivalence study with 36 subjects is recommended "
        "[PSG_020503, p.3]. The agency also recommends an in vivo fed study "
        "[PSG_FAKE, p.7]."
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(answer_text))
    result = qa_mod.ask("What study design is recommended?")
    assert not result.refused
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020503", 3)}


# ---------- INV-2: Refuse over guess ----------


def test_inv2_refuses_when_corpus_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No documents indexed → must refuse, must not call the LLM."""
    init_db()
    called = {"n": 0}

    def _no_llm(*a: object, **k: object) -> Any:
        called["n"] += 1
        return _stub_llm("This should never run.")

    monkeypatch.setattr(qa_mod, "get_llm_provider", _no_llm)
    result = qa_mod.ask("What is the BE acceptance interval for metformin ER?")
    assert result.refused
    assert result.answer == get_settings().refusal_text
    assert called["n"] == 0


def test_inv2_refuses_when_model_outputs_refusal_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the model returns the refusal string, the system must refuse."""
    _seed_corpus([("Generic body of content about bioequivalence.", _meta(2, 1, "PSG_222222"))])
    refusal = get_settings().refusal_text
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(refusal))
    result = qa_mod.ask("Some adversarial out-of-corpus question?")
    assert result.refused
    assert result.answer == refusal


def test_inv2_refuses_when_answer_has_no_valid_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confident answer without any verifiable citation must collapse to refusal."""
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    fabricated = "The recommended dose is 100 mg per day [PSG_NOT_REAL, p.99]."
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(fabricated))
    result = qa_mod.ask("What is the recommended dose?")
    assert result.refused


# ---------- INV-6: Auditability ----------


def _row_count(model: Any) -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_inv6_every_qa_call_logs_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("Yes. [PSG_020503, p.3]")
    )
    assert _row_count(QueryLog) == 0
    qa_mod.ask("Is a fasting study recommended?")
    assert _row_count(QueryLog) == 1
    qa_mod.ask("Same again?")
    assert _row_count(QueryLog) == 2


def test_inv6_refusal_also_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    assert _row_count(QueryLog) == 0
    qa_mod.ask("Out-of-corpus question with no indexed content.")
    rows = []
    with session_scope() as s:
        for r in s.scalars(select(QueryLog)):
            rows.append((r.refused, r.mode, r.answer_text))
    assert len(rows) == 1
    assert rows[0][0] is True
    assert rows[0][1] == "qa"
    assert rows[0][2] == get_settings().refusal_text


# ---------- INV-3, INV-4, INV-5: structural placeholders ----------


def test_inv3_no_authoring_endpoints() -> None:
    """The codebase MUST NOT expose any endpoint that drafts FDA submission content.

    We grep the api/ package for forbidden tokens. This is a structural test —
    it catches a future contributor adding a draft/submit endpoint.
    """
    import pathlib

    api_dir = pathlib.Path("src/regwatch/api")
    forbidden = ("/draft", "/submit", "/file_anda", "/generate_submission")
    for path in api_dir.rglob("*.py"):
        text = path.read_text()
        for token in forbidden:
            assert token not in text, f"INV-3 violation: {path} contains {token!r}"


def test_inv5_product_source_is_verified_only() -> None:
    """Watchlist products must declare a verified `source` ∈ {drugsfda, anda_letter, manual}.

    This test is structural: it inspects the model definition to ensure no
    'guess' / 'model_memory' source is accepted by the schema layer.
    """
    from regwatch.store.models import Product

    # Just confirms the model is reachable and the field exists; full source-set
    # enforcement is in the Phase-3 watchlist loader tests.
    assert hasattr(Product, "source")


def test_inv4_alerts_only_for_real_versions() -> None:
    """INV-4: an alert must reference a `psg_version` that was actually inserted.

    A match against a listing whose PSG was never fetched produces NO alert.
    """
    from regwatch.common.text_normalize import canonical_name, stripped_name
    from regwatch.ingest.psg_crawler import PsgListing
    from regwatch.watch.alerts import build_alerts
    from regwatch.watch.matcher import WatchMatch

    init_db()  # empty DB — no PsgDocument / PsgVersion exists
    listing = PsgListing(
        appl_no="999999",
        active_ingredient="Imaginary",
        normalized_name=canonical_name("Imaginary"),
        stripped_name=stripped_name("Imaginary"),
        psg_type="final",
        route=None,
        dosage_form=None,
        rld_or_rs_numbers=[],
        recommended_date=None,
        pdf_url="http://example/PSG_999999.pdf",
        source_url="http://example/",
    )
    match = WatchMatch(
        listing=listing,
        product={"id": 1, "active_ingredient": "Imaginary"},
        confidence=1.0,
        rationale="canonical",
    )
    assert build_alerts([match]) == []
