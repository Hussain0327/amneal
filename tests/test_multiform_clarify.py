"""Multi-form clarify guard — same-drug multi-document blending is the audit's
#1 correctness blocker.

The resolver pins only ``normalized_name``, but ~1 in 5 drugs span multiple
dosage forms/routes. Blending a transdermal-gel PSG and a vaginal-tablet PSG
for "estradiol" into one LLM context lets a wrong-form citation validate as
good — and the blend is invisible (citation labels are appl-number-only). This
suite locks:

  - PRE-retrieval: a resolved multi-form drug CLARIFIES (one option per combo)
    before retrieving — one query_log row (INV-6), zero citations;
  - same-combo docs stay answerable (beclomethasone has two docs in ONE combo —
    the guard keys on combos, never doc_id, so it must not split them);
  - a single-form drug proceeds to a normal cited answer (no clarify);
  - POST-retrieval defense-in-depth: passages spanning >1 combo clarify even when
    a caller bypassed the pre-retrieval guard;
  - SESSION CARRYOVER: clarify → click a combo option → answer → follow-up →
    answer, with NO second clarify (the chosen form persists);
  - cross-drug: a combination product (different normalized_name) is never
    conflated with the single-ingredient drug (INV-9 stays strong).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from config.settings import get_settings
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.retriever import RetrievedPassage
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion, QueryLog
from regwatch.store.vector_store import add_chunks

pytestmark = pytest.mark.invariants


# (text, appl_no, normalized_name, dosage_form, route, page)
_Row = tuple[str, str, str, str, str, int]


def _seed(rows: list[_Row]) -> None:
    """Seed both the SQL catalog (psg_document + psg_version, enumerated by the
    pre-retrieval guard) and Chroma, keeping doc_id/version_id consistent so the
    current-version retrieval scope finds the seeded chunks."""
    init_db()
    doc_ids: dict[str, int] = {}
    version_ids: dict[str, int] = {}
    with session_scope() as s:
        for _text, appl, name, form, route, _page in rows:
            if appl in doc_ids:
                continue
            doc = PsgDocument(
                active_ingredient=name.title(),
                normalized_name=name,
                dosage_form=form,
                route=route,
                appl_no=appl,
                psg_type="draft",
                recommended_date="2026-01-01",
                source_url=f"http://example/PSG_{appl}.pdf",
                content_hash=f"hash-{appl}",
            )
            s.add(doc)
            s.flush()
            assert doc.id is not None
            ver = PsgVersion(psg_document_id=doc.id, content_hash=f"hash-{appl}")
            s.add(ver)
            s.flush()
            assert ver.id is not None
            doc_ids[appl] = doc.id
            version_ids[appl] = ver.id

    emb = get_embedding_provider()
    texts = [t for t, _, _, _, _, _ in rows]
    add_chunks(
        ids=[f"{appl}-{page}" for _, appl, _, _, _, page in rows],
        embeddings=emb.embed(texts),
        documents=texts,
        metadatas=[
            {
                "doc_id": doc_ids[appl],
                "version_id": version_ids[appl],
                "page": page,
                "normalized_name": name,
                "appl_no": appl,
                "source_url": f"http://example/PSG_{appl}.pdf",
                "section_path": "",
                "dosage_form": form,
                "route": route,
                "psg_type": "draft",
            }
            for text, appl, name, form, route, page in rows
        ],
    )


# estradiol-like multi-form drug: transdermal gel vs. vaginal tablet (2 combos).
_MULTIFORM: list[_Row] = [
    ("estradiol transdermal gel BE study guidance", "020001", "estradiol", "Gel", "Transdermal", 1),
    ("estradiol vaginal tablet BE study guidance", "020002", "estradiol", "Tablet", "Vaginal", 1),
]

# beclomethasone-like: two docs in the SAME (dosage_form, route) combo. The guard
# must NOT split these — current green gold answers cite across same-combo docs.
_SAME_COMBO: list[_Row] = [
    (
        "beclomethasone SAC fasting two-way crossover",
        "020911",
        "beclomethasone dipropionate",
        "Aerosol, Metered",
        "Inhalation",
        1,
    ),
    (
        "beclomethasone acceptance criteria SAC",
        "207921",
        "beclomethasone dipropionate",
        "Aerosol, Metered",
        "Inhalation",
        1,
    ),
]

# single-form drug — exactly one combo, answers normally.
_SINGLE_FORM: list[_Row] = [
    (
        "albuterol fasting single-dose two-way crossover BE study",
        "020503",
        "albuterol sulfate",
        "Aerosol, Metered",
        "Inhalation",
        4,
    ),
]


class _CitingStub:
    """Cites the first passage it was handed — faithful by construction."""

    name = "stub"

    def complete(self, messages: list[Any], **_kw: object) -> LLMResponse:
        user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        region = user.split("Source passages:\n", 1)[-1].split("\n\nAnswer with citations", 1)[0]
        first = next((b.strip() for b in region.split("\n---\n") if b.strip()), "")
        head = first.partition("\n")[0]
        m = re.search(r"\[([^,\]]+),\s*p\.(\d+)\]", head)
        if not m:
            return LLMResponse(text=get_settings().refusal_text, model=self.name)
        return LLMResponse(
            text=f"Recommended BE study design summary [{m.group(1)}, p.{m.group(2)}].",
            model=self.name,
        )


def _stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _CitingStub())


def _query_log_count() -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(QueryLog)) or 0)


# ---------- 1. PRE-retrieval guard ----------
def test_multiform_drug_clarifies_before_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    _seed(_MULTIFORM)
    before = _query_log_count()

    r = qa_mod.ask("What bioequivalence study design does FDA recommend for estradiol?")

    assert r.status == "clarify"
    assert r.reason == "multi_form"  # the multi-form guard fired (not another clarify)
    assert not r.refused
    assert not r.citations  # never blends forms / fabricates
    assert not r.retrieved  # the guard fires BEFORE retrieval
    # One option per combo, each pinning normalized_name + dosage_form + route.
    combos = {(o.filters["dosage_form"], o.filters["route"]) for o in r.clarify if o.filters}
    assert combos == {("Gel", "Transdermal"), ("Tablet", "Vaginal")}
    assert all(o.filters and o.filters["normalized_name"] == "estradiol" for o in r.clarify)
    assert all("estradiol" in o.label.lower() for o in r.clarify)
    assert _query_log_count() == before + 1  # INV-6


def test_selecting_one_combo_answers_with_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    _seed(_MULTIFORM)

    # Simulate the user clicking the transdermal-gel option (filters round-trip).
    r = qa_mod.ask(
        "What bioequivalence study design does FDA recommend for estradiol?",
        filters={"normalized_name": "estradiol", "dosage_form": "Gel", "route": "Transdermal"},
    )

    assert r.status == "answer"
    assert not r.refused
    # Constrained to the chosen form only — no vaginal-tablet leak.
    assert {p["short_name"] for p in r.retrieved} == {"PSG_020001"}
    assert {(c.short_name, c.page) for c in r.citations} == {("PSG_020001", 1)}


# ---------- 2. same-combo docs stay answerable ----------
def test_same_combo_multiple_docs_answer_without_clarify(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    _seed(_SAME_COMBO)

    r = qa_mod.ask(
        "What type of study does the beclomethasone dipropionate PSG recommend?",
    )

    # Two docs, ONE combo → no clarify; the guard keys on combos, not doc_id.
    assert r.status == "answer"
    assert not r.refused
    assert r.citations


# ---------- 2b. form-explicit question pins the form (no pointless clarify) ----------
def test_form_explicit_question_answers_without_clarify(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    _seed(_MULTIFORM)

    # The question NAMES the form ("transdermal gel"), so the multi-form guard must
    # pin that combo and ANSWER rather than pay a pointless clarify hop (and on the
    # full catalog this is what keeps form-explicit answerable gold items answering).
    r = qa_mod.ask("What study design does the estradiol transdermal gel PSG recommend?")

    assert r.status == "answer"
    assert not r.refused
    # Constrained to the named form only — no vaginal-tablet leak.
    assert {p["short_name"] for p in r.retrieved} == {"PSG_020001"}
    assert {(c.short_name, c.page) for c in r.citations} == {("PSG_020001", 1)}


# ---------- 3. single-form drug proceeds ----------
def test_single_form_drug_answers_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    _seed(_SINGLE_FORM)

    r = qa_mod.ask("What study design does the albuterol sulfate PSG recommend?")

    assert r.status == "answer"
    assert not r.refused
    assert {(c.short_name, c.page) for c in r.citations} == {("PSG_020503", 4)}


# ---------- 4. POST-retrieval defense in depth ----------
def test_mixed_form_passages_clarify_when_resolver_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A caller bypassed the pre-retrieval guard and handed back passages from one
    # product but TWO dosage forms. The post-retrieval guard must clarify by form.
    init_db()

    def _passage(appl: str, form: str, route: str) -> RetrievedPassage:
        return RetrievedPassage(
            chunk_id=f"{appl}-1",
            text="BE study guidance text.",
            score=0.9,
            doc_id=1,
            version_id=10,
            page=1,
            section_path=None,
            normalized_name="estradiol",
            source_url=f"http://example/PSG_{appl}.pdf",
            short_name=f"PSG_{appl}",
            metadata={"dosage_form": form, "route": route},
        )

    mixed = [
        _passage("020001", "Gel", "Transdermal"),
        _passage("020002", "Tablet", "Vaginal"),
    ]
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: mixed)
    monkeypatch.setattr(qa_mod, "current_dosage_form_routes", lambda *a, **k: [])

    before = _query_log_count()
    r = qa_mod.ask(
        "What study design does the PSG recommend?",
        filters={"normalized_name": "estradiol"},
    )

    assert r.status == "clarify"
    assert not r.refused
    assert not r.citations  # never blends forms across passages
    combos = {(o.filters["dosage_form"], o.filters["route"]) for o in r.clarify if o.filters}
    assert combos == {("Gel", "Transdermal"), ("Tablet", "Vaginal")}
    assert _query_log_count() == before + 1  # INV-6
    assert r.answer != get_settings().refusal_text


# ---------- 5. session carryover (no second clarify) ----------
def test_followup_after_combo_selection_does_not_reclarify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch)
    _seed(_MULTIFORM)

    # Turn 1: bare multi-form question → clarify.
    first = qa_mod.ask("What bioequivalence study design does FDA recommend for estradiol?")
    assert first.status == "clarify"
    assert first.session_id

    # Turn 2: user clicks the transdermal-gel option (filters round-trip).
    second = qa_mod.ask(
        "What bioequivalence study design does FDA recommend for estradiol?",
        filters={"normalized_name": "estradiol", "dosage_form": "Gel", "route": "Transdermal"},
        session_id=first.session_id,
    )
    assert second.status == "answer"
    assert {(c.short_name, c.page) for c in second.citations} == {("PSG_020001", 1)}

    # Turn 3: a follow-up must NOT re-trigger the multi-form clarify — the chosen
    # form persists in the session.
    third = qa_mod.ask(
        "What about dissolution?",
        session_id=first.session_id,
    )
    assert third.status == "answer"
    assert third.session_id == first.session_id
    assert {p["short_name"] for p in third.retrieved} == {"PSG_020001"}

    # The session carried form + route, not just the product.
    from regwatch.store.models import ChatSession

    with session_scope() as s:
        session = s.get(ChatSession, first.session_id)
        assert session is not None
        carried = dict(session.active_filters_json)
        assert carried["normalized_name"] == "estradiol"
        assert carried["dosage_form"] == "Gel"
        assert carried["route"] == "Transdermal"


# ---------- 6. combination product not conflated (INV-9) ----------
def test_combination_product_not_conflated_with_single(monkeypatch: pytest.MonkeyPatch) -> None:
    # "estradiol; levonorgestrel" is a DIFFERENT normalized_name. With its own
    # single combo it answers normally — it is never folded into estradiol's combos.
    _stub(monkeypatch)
    _seed(
        [
            (
                "estradiol levonorgestrel transdermal film BE study",
                "020003",
                "estradiol; levonorgestrel",
                "Film, Extended Release",
                "Transdermal",
                1,
            ),
        ]
    )

    r = qa_mod.ask(
        "What study design does the PSG recommend?",
        filters={"normalized_name": "estradiol; levonorgestrel"},
    )

    assert r.status == "answer"
    assert not r.refused
    assert {(c.short_name, c.page) for c in r.citations} == {("PSG_020003", 1)}
