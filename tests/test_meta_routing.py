"""#2 Meta-question routing — the SAFETY-CRITICAL slice.

A regulatory question that happens to contain a meta phrase ("what do you cover")
must NEVER reach the uncited meta path (INV-1/INV-2). These tests pin that:

  T1 routing-veto    — a named in-corpus drug skips meta (the hard veto fires).
  T2 meta-uncited    — a true meta question answers with status='meta', no LLM,
                       no citations, no retrieval.
  T3 no-drug-fact-leak — the meta answer carries only system facts (corpus /
                       watchlist / digest), never BE/dissolution regulatory prose.
  T4 meta-audited    — exactly ONE QueryLog row, status='meta' (extends INV-6).
  T5 meta-audit-write-failure — a transient audit DB error degrades (audit_id=-1,
                       status stays 'meta') instead of a naked 500, mirroring the
                       _refuse/_clarify degrade contract.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks
from regwatch.watch.alerts import Alert, _persist_alerts
from regwatch.watch.watchlist import add_manual_product

pytestmark = pytest.mark.invariants


# ---------- Helpers ----------


def _seed(names: list[str]) -> None:
    """Seed the corpus with one bioequivalence/dissolution passage per product."""
    init_db()
    embedder = get_embedding_provider()
    texts = [
        f"Recommended bioequivalence study and dissolution method for {n}: "
        f"a fasting single-dose crossover with a 90% confidence interval."
        for n in names
    ]
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


def _counting_retrieve(counter: dict[str, int]) -> Any:
    """A retrieve() replacement that bumps a counter if retrieval ever runs."""

    def _retrieve(*a: object, **k: object) -> list[Any]:
        counter["n"] += 1
        return []

    return _retrieve


def _row_count(model: Any) -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


# ---------- T1 routing-veto: a named in-corpus drug is NEVER meta ----------


@pytest.mark.parametrize(
    "question",
    [
        "what BE study do you cover for atorvastatin?",
        "how do I scope the metformin dissolution?",
    ],
)
def test_t1_named_drug_vetoes_meta(question: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A regulatory question carrying a meta phrase but naming an in-corpus drug
    must skip the meta path — the hard veto fires."""
    # "scope" is one of the scope-warning trigger words only as a phrase; these
    # questions name a real drug, so they must reach the grounded path, not meta.
    _seed(["atorvastatin calcium", "metformin hydrochloride"])
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()

    def _grounded_llm(*a: object, **k: object) -> Any:
        class _LLM:
            name = "stub"

            def complete(self, *a: object, **kw: object) -> LLMResponse:
                # Honest grounded citation to whichever product was retrieved.
                return LLMResponse(text="Yes. [PSG_000001, p.1]", model="stub")

        return _LLM()

    monkeypatch.setattr(qa_mod, "get_llm_provider", _grounded_llm)

    result = qa_mod.ask(question)

    # The veto held: this never routed to the uncited meta answer.
    assert result.status != "meta"


def test_t1_named_drug_overrides_a_matching_meta_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing veto: a question that MATCHES a meta phrase
    ("what do you cover") but ALSO names an in-corpus drug must be vetoed off the
    meta path and routed to the grounded cite-or-refuse path. This is the exact
    INV-1/INV-2 hole — if the veto regresses, this turns into status='meta'.
    """
    # Two products so resolution is genuinely BY NAME (atorvastatin), not the
    # single-product-corpus fallback — this proves the named-drug veto itself.
    _seed(["atorvastatin calcium", "metformin hydrochloride"])
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
    from regwatch.retrieve.resolver import resolve_product

    question = "what do you cover for atorvastatin?"
    # Precondition: the meta phrase matches AND the drug resolves by name — so
    # ONLY the veto (not a phrase miss) keeps this off the meta path.
    assert qa_mod._is_meta_request(question)
    resolution = resolve_product(question)
    assert resolution.status == "resolved"
    assert resolution.normalized_name == "atorvastatin calcium"
    assert resolution.by_name is True

    def _grounded_llm(*a: object, **k: object) -> Any:
        class _LLM:
            name = "stub"

            def complete(self, *a: object, **kw: object) -> LLMResponse:
                return LLMResponse(text="Yes. [PSG_000001, p.1]", model="stub")

        return _LLM()

    monkeypatch.setattr(qa_mod, "get_llm_provider", _grounded_llm)

    result = qa_mod.ask(question)

    assert result.status != "meta", "named-drug veto failed — meta phrase reached the uncited path"


# ---------- T2 meta-uncited: a true meta question is answered, no LLM ----------


def test_t2_meta_uncited_no_llm_no_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """'what products do you cover?' => status=='meta', refused False, citations
    [], LLM call counter == 0, and retrieval never runs."""
    _seed(["atorvastatin calcium", "metformin hydrochloride"])
    llm_calls = {"n": 0}
    retrieve_calls = {"n": 0}
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm(llm_calls))
    monkeypatch.setattr(qa_mod, "retrieve", _counting_retrieve(retrieve_calls))

    result = qa_mod.ask("what products do you cover?")

    assert result.status == "meta"
    assert result.refused is False
    assert result.citations == []
    assert result.retrieved == []
    assert llm_calls["n"] == 0, "the meta path must call NO LLM"
    assert retrieve_calls["n"] == 0, "the meta path must run NO retrieval"


# ---------- T3 no-drug-fact-leak: meta answer is system facts only ----------


def test_t3_meta_answer_is_system_facts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The meta answer text contains only system facts (counts, product names,
    watchlist, digest) — never the BE/dissolution regulatory prose that lives in
    the seeded corpus passages."""
    _seed(["atorvastatin calcium", "metformin hydrochloride"])
    # A monitored product distinct from the askable corpus — so we can prove the
    # answer keeps the two separate and never conflates them.
    add_manual_product(
        active_ingredient="Romidepsin",
        dosage_form="Injection",
        route="Intravenous",
        rld_name="Istodax",
        rld_application_number="208574",
        company_status="approved",
        source="manual",
        source_url=None,
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm({"n": 0}))

    result = qa_mod.ask("what do you cover and what do you watch?")
    answer = result.answer.lower()

    # System facts ARE present: the askable corpus AND the watchlist, distinctly.
    assert "atorvastatin" in answer
    assert "romidepsin" in answer  # the monitored (watchlist) product
    assert "corpus" in answer  # corpus is labeled
    assert "watch" in answer  # watchlist is labeled separately
    # Regulatory prose from the seeded passages NEVER leaks into the meta answer.
    for forbidden in ("bioequivalence study", "dissolution method", "confidence interval"):
        assert forbidden not in answer, f"regulatory prose leaked into meta answer: {forbidden!r}"


# ---------- T3b change/digest branch: diff_summary prose must NOT leak ----------

# A diff_summary as produced by process/change_detector.summarize_change: raw LLM
# output that quotes PSG passages and states BE/dissolution facts. It is a
# regulatory claim, NOT a system fact, so it must never reach the uncited meta
# answer on the "what changed?" branch (INV-1).
_PROSE_DIFF_SUMMARY = (
    "BE study changed to a fasting single-dose crossover; the dissolution method "
    'now requires a 90% confidence interval and quotes "Section II.A, p.3".'
)


def test_t3b_what_changed_does_not_leak_diff_summary_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 'what changed?' meta branch reports only NON-PROSE system facts
    (product name + capture date) and points to the cited Watch feed — it must
    NEVER embed the alert's diff_summary, which is LLM output / raw passage text
    carrying BE/dissolution regulatory prose. This exercises the change/digest
    branch that T3 (a non-change phrase) never reaches.
    """
    _seed(["atorvastatin calcium", "metformin hydrochloride"])
    # Seed a DURABLE alert whose diff_summary is regulatory prose — exactly the
    # normal prod state (live durable alerts present).
    _persist_alerts(
        [
            Alert(
                product_id=1,
                active_ingredient="Atorvastatin Calcium",
                listing_appl_no="00001",
                listing_psg_type="draft",
                psg_document_id=1,
                psg_version_id=10,
                captured_at="2026-06-10T00:00:00+00:00",
                diff_summary=_PROSE_DIFF_SUMMARY,
                confidence=0.9,
                rationale="appl_no match",
                source_url="http://example/0.pdf",
            )
        ]
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm({"n": 0}))

    result = qa_mod.ask("what changed?")
    answer = result.answer

    # Routed to the uncited meta path, as before.
    assert result.status == "meta"
    assert result.refused is False
    assert result.citations == []
    # The NON-PROSE system facts ARE present: the product name + its capture date,
    # plus a pointer to the cited Watch feed for detail.
    assert "Atorvastatin Calcium" in answer
    assert "2026-06-10" in answer
    assert "watch feed" in answer.lower()
    # The regulatory prose from diff_summary NEVER leaks into the uncited answer.
    assert _PROSE_DIFF_SUMMARY not in answer
    lowered = answer.lower()
    for forbidden in (
        "fasting single-dose crossover",
        "dissolution method",
        "confidence interval",
        "section ii.a",
    ):
        assert (
            forbidden not in lowered
        ), f"diff_summary prose leaked into meta answer: {forbidden!r}"


# ---------- T4 meta-audited: exactly one row, status='meta' (INV-6) ----------


def test_t4_meta_writes_exactly_one_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Extends INV-6: a meta turn writes EXACTLY one QueryLog row, status='meta',
    refused False, no citations — same single-audit contract as every terminal
    handler."""
    # Seed >=2 products: a single-product corpus makes resolve_product fall back
    # to "resolved", which (correctly) vetoes meta. Production has ~1,795 PSGs, so
    # the realistic case is a multi-product corpus where a no-drug meta question
    # resolves to "none" and the meta gate fires.
    _seed(["atorvastatin calcium", "metformin hydrochloride"])
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm({"n": 0}))
    assert _row_count(QueryLog) == 0

    result = qa_mod.ask("what can I ask about?")

    assert result.status == "meta"
    assert _row_count(QueryLog) == 1
    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.status == "meta"
        assert row.refused is False
        assert list(row.citations_json or []) == []
        assert list(row.retrieved_json or []) == []
        assert dict(row.route_json)["response_mode"] == "meta"


# ---------- T5 meta-audit-write-failure degrades, never a naked 500 ----------


def test_t5_meta_audit_write_failure_degrades_audit_id_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_meta reaches the QueryLog write through the same _decline ceremony as
    _refuse/_clarify. Mirrors test_audit_write_failure_degrades_to_error_refusal_
    not_500 (test_grounded_qa_citations.py): a transient DB error on the audit
    write must degrade to audit_id=-1 (logged + Sentry-captured) rather than
    propagate as an unhandled 500 -- the meta answer itself is safe, uncited
    system-state text, so (unlike the LLM-grounded path) it is still returned."""
    _seed(["atorvastatin calcium", "metformin hydrochloride"])
    monkeypatch.setattr(qa_mod, "get_llm_provider", _counting_llm({"n": 0}))

    def _boom(**kwargs: object) -> int:
        raise RuntimeError("simulated audit db outage")

    monkeypatch.setattr(qa_mod, "log_query", _boom)

    result = qa_mod.ask("what can I ask about?")

    assert result.status == "meta"
    assert result.refused is False
    assert result.citations == []
    assert result.audit_id == -1
