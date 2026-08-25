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
from sqlalchemy import event, func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.retriever import RetrievedPassage
from regwatch.store.db import get_engine, init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion, QueryLog
from regwatch.store.vector_store import add_chunks
from tests.conftest import synth_turn_json

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


def _first_passage_pair(messages: list[Any]) -> tuple[str, int] | None:
    """The (short_name, page) header of the FIRST passage sent this turn."""
    user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    region = user.split("<untrusted_source_passages>\n", 1)[-1].split(
        "\n</untrusted_source_passages>", 1
    )[0]
    first = next((b.strip() for b in region.split("\n---\n") if b.strip()), "")
    head = first.partition("\n")[0]
    m = re.search(r"\[([^,\]]+),\s*p\.(\d+)\]", head)
    return (m.group(1), int(m.group(2))) if m else None


class _CitingStub:
    """Cites the first passage it was handed — faithful by construction.

    Returns the STRUCTURED turn object the synthesizer contract now requires
    (one claim, one declared cite); the renderer, not the model, writes the
    citation marker. A model that cannot cite declines with turn_type
    "NO_EVIDENCE" -- the refusal STRING is no longer a synthesizer output.
    """

    name = "stub"

    def complete(self, messages: list[Any], **_kw: object) -> LLMResponse:
        pair = _first_passage_pair(messages)
        if pair is None:
            return LLMResponse(text=synth_turn_json(turn_type="NO_EVIDENCE"), model=self.name)
        return LLMResponse(
            text=synth_turn_json([("Recommended BE study design summary", [pair])]),
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


def test_wrong_form_citation_after_pin_is_dropped_not_restamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-1, the reason this whole file exists: once a combo is pinned, a claim
    citing the OTHER form's PSG must be dropped WHOLE, never re-stamped onto the
    pinned passage. PSG_020002 is a real seeded document, so only "was it sent
    THIS turn" can reject it. Zero admitted claims -> refuse with no citations.
    """
    _stub(monkeypatch)
    _seed(_MULTIFORM)

    class _WrongFormStub:
        name = "stub"

        def complete(self, messages: list[Any], **_kw: object) -> LLMResponse:
            # Cites the vaginal-tablet PSG while only the gel PSG was retrieved.
            return LLMResponse(
                text=synth_turn_json(
                    [("Recommended BE study design summary", [("PSG_020002", 1)])]
                ),
                model=self.name,
            )

    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _WrongFormStub())

    r = qa_mod.ask(
        "What bioequivalence study design does FDA recommend for estradiol?",
        filters={"normalized_name": "estradiol", "dosage_form": "Gel", "route": "Transdermal"},
    )

    assert r.refused is True
    assert r.status == "refused"
    assert r.reason == "no_valid_citations"
    assert r.citations == []
    # Neither form's marker may appear: the claim is not re-stamped onto the
    # pinned gel passage, and the unretrieved tablet PSG is not echoed either.
    assert "PSG_020001" not in r.answer
    assert "PSG_020002" not in r.answer
    # INV-6: the decline is audited, and the unvalidated claim never lands in the
    # persisted answer. OD-5's operator half: a DECLINE row carries the gate
    # ledger too, so the drop reason and the offending (short_name, page) are
    # forensically recoverable from the DB -- not only from a structlog line.
    with session_scope() as s:
        row = s.get(QueryLog, r.audit_id)
        assert row is not None
        assert row.refused is True
        ledger = row.route_json["turn"]
        assert ledger["verdict"] == "no_valid_citations"
        assert (ledger["emitted"], ledger["admitted"], ledger["dropped"]) == (1, 0, 1)
        claims = ledger["claims"]
        assert [c["drop_reason"] for c in claims] == ["unknown_citation"]
        assert claims[0]["bad_cites"] == ["PSG_020002,p.1"]
        assert list(row.citations_json or []) == []
        assert "PSG_020002" not in row.answer_text


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


# --- _combo_from_question: form-pinning correctness on the oral-tablet mass ---
# These lock the two corrections that stop needless oral multi-form clarifies and
# close the wrong-form-citation hole (the stopword "for" colliding with the real
# catalog form "Tablet, For Suspension"). Pure-function tests, no DB.


def test_combo_for_stopword_does_not_pin_for_suspension() -> None:
    # "...recommend FOR apixaban tablet" must pin plain (Tablet), never the
    # "Tablet, For Suspension" sibling on the stray preposition "for" (INV-1:
    # a wrong-form pin would cite the wrong PSG).
    combos = [("Tablet", "Oral"), ("Tablet, For Suspension", "Oral")]
    q = "What BE study design does FDA recommend for apixaban tablet?"
    assert qa_mod._combo_from_question(q, combos) == ("Tablet", "Oral")


def test_combo_plain_tablet_pins_over_extended_release_sibling() -> None:
    # "...for amantadine tablet" covers (Tablet) completely but (Tablet, ER)
    # partially -> pin plain Tablet instead of a pointless clarify hop.
    combos = [("Tablet", "Oral"), ("Tablet, Extended Release", "Oral")]
    q = "What BE study design does FDA recommend for amantadine hydrochloride tablet?"
    assert qa_mod._combo_from_question(q, combos) == ("Tablet", "Oral")


def test_combo_extended_release_still_pins_er_variant() -> None:
    combos = [("Tablet", "Oral"), ("Tablet, Extended Release", "Oral")]
    q = "BE study for the extended release tablet"
    assert qa_mod._combo_from_question(q, combos) == ("Tablet, Extended Release", "Oral")


def test_combo_form_silent_question_still_clarifies() -> None:
    combos = [("Tablet", "Oral"), ("Tablet, Extended Release", "Oral")]
    q = "What bioequivalence study design does FDA recommend for amantadine?"
    assert qa_mod._combo_from_question(q, combos) is None


def test_combo_ambiguous_short_form_token_still_clarifies() -> None:
    # "tablet" fits ER and ODT equally completely -> still ambiguous -> clarify.
    combos = [("Tablet, Extended Release", "Oral"), ("Tablet, Orally Disintegrating", "Oral")]
    q = "BE study for the tablet"
    assert qa_mod._combo_from_question(q, combos) is None


def test_combo_route_only_mention_shared_by_combos_still_clarifies() -> None:
    # Both combos share the route "Inhalation" and the question names ONLY that
    # route word -- no dosage form. The completeness tie-break must not silently
    # pin the shorter (Solution) combo; the multi-form clarify has to fire.
    combos = [("Aerosol, Metered", "Inhalation"), ("Solution", "Inhalation")]
    q = "What BE study does FDA recommend for albuterol sulfate inhalation?"
    assert qa_mod._combo_from_question(q, combos) is None


def test_combo_discriminating_route_still_pins() -> None:
    # A route word unique to ONE combo is a real disambiguator (no tie to
    # break), so it still pins -- the route-only guard must not overreach.
    combos = [("Gel", "Transdermal"), ("Tablet", "Vaginal")]
    q = "What BE study design does FDA recommend for transdermal estradiol?"
    assert qa_mod._combo_from_question(q, combos) == ("Gel", "Transdermal")


# ---------- 7. failure paths and audit fidelity ----------


def test_form_catalog_db_error_degrades_to_audited_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB error on the combo-enumeration CORRECTNESS guard must become the
    audited, fixed-copy status='error' refusal (like a provider failure) --
    never an unaudited 500 the stream fallback re-runs into the same down DB."""
    init_db()

    def _boom(*a: object, **k: object) -> list[tuple[str, str]]:
        raise RuntimeError("simulated catalog db outage")

    monkeypatch.setattr(qa_mod, "current_dosage_form_routes", _boom)
    before = _query_log_count()

    r = qa_mod.ask(
        "What BE study design is recommended?",
        filters={"normalized_name": "estradiol"},
    )

    assert r.refused is True
    assert r.status == "error"
    assert r.citations == []
    assert "temporarily unavailable" in r.answer
    assert _query_log_count() == before + 1  # INV-6: audited despite the DB error


def _bypass_passage(appl: str, name: str, form: str, route: str) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=f"{appl}-1",
        text="BE study guidance text.",
        score=0.9,
        doc_id=1,
        version_id=10,
        page=1,
        section_path=None,
        normalized_name=name,
        source_url=f"http://example/PSG_{appl}.pdf",
        short_name=f"PSG_{appl}",
        metadata={"dosage_form": form, "route": route},
    )


def test_post_retrieval_multiform_clarify_audits_retrieved_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The multi_form backstop fires AFTER retrieval, so its audit row (and the
    returned QAResult) must record the passages that tripped it -- retrieved=[]
    would drop the forensic evidence exactly when the tripwire caught a bypass."""
    init_db()
    mixed = [
        _bypass_passage("020001", "estradiol", "Gel", "Transdermal"),
        _bypass_passage("020002", "estradiol", "Tablet", "Vaginal"),
    ]
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: mixed)
    monkeypatch.setattr(qa_mod, "current_dosage_form_routes", lambda *a, **k: [])

    r = qa_mod.ask(
        "What study design does the PSG recommend?",
        filters={"normalized_name": "estradiol"},
    )

    assert r.status == "clarify"
    assert r.reason == "multi_form"
    assert {p["chunk_id"] for p in r.retrieved} == {"020001-1", "020002-1"}
    with session_scope() as s:
        row = s.get(QueryLog, r.audit_id)
        assert row is not None
        assert {p["chunk_id"] for p in row.retrieved_json} == {"020001-1", "020002-1"}


def test_post_retrieval_mixed_products_clarify_audits_retrieved_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same audit-fidelity property for the mixed_products tripwire."""
    init_db()
    mixed = [
        _bypass_passage("020001", "estradiol", "Gel", "Transdermal"),
        _bypass_passage("020911", "beclomethasone dipropionate", "Aerosol, Metered", "Inhalation"),
    ]
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: mixed)
    monkeypatch.setattr(qa_mod, "current_dosage_form_routes", lambda *a, **k: [])

    r = qa_mod.ask(
        "What study design does the PSG recommend?",
        filters={"normalized_name": "estradiol"},
    )

    assert r.status == "clarify"
    assert r.reason == "mixed_products"
    assert {p["chunk_id"] for p in r.retrieved} == {"020001-1", "020911-1"}
    with session_scope() as s:
        row = s.get(QueryLog, r.audit_id)
        assert row is not None
        assert {p["chunk_id"] for p in row.retrieved_json} == {"020001-1", "020911-1"}


def test_pre_retrieval_clarify_still_audits_empty_retrieved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-retrieval clarifies genuinely retrieved nothing -- their audit row
    must keep recording retrieved=[] (the passages parameter defaults off)."""
    _stub(monkeypatch)
    _seed(_MULTIFORM)

    r = qa_mod.ask("What bioequivalence study design does FDA recommend for estradiol?")

    assert r.status == "clarify"
    assert r.retrieved == []
    with session_scope() as s:
        row = s.get(QueryLog, r.audit_id)
        assert row is not None
        assert list(row.retrieved_json or []) == []


def test_followup_reads_the_session_row_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A carried-over follow-up needs the session filters at two sites (product,
    then form/route); the turn must READ the ChatSession row exactly ONCE.

    Counts real SQL rather than calls into a module-level reader: ask() now
    pre-loads the turn's context in the single transaction that opens the turn,
    so a counter on qa_mod.get_session_filters would sit on a function this
    path no longer calls and would pass at zero, for the wrong reason.
    """
    _stub(monkeypatch)
    # Two products, so the follow-up does NOT hit the single-product-corpus
    # fallback -- the resolver returns none and BOTH carry-over sites run.
    _seed(_MULTIFORM + _SINGLE_FORM)

    first = qa_mod.ask("What bioequivalence study design does FDA recommend for estradiol?")
    assert first.status == "clarify"
    second = qa_mod.ask(
        "What bioequivalence study design does FDA recommend for estradiol?",
        filters={"normalized_name": "estradiol", "dosage_form": "Gel", "route": "Transdermal"},
        session_id=first.session_id,
    )
    assert second.status == "answer"

    statements: list[str] = []

    def _capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        statements.append(" ".join(statement.split()))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        third = qa_mod.ask("What about dissolution?", session_id=first.session_id)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert third.status == "answer"
    assert {p["short_name"] for p in third.retrieved} == {"PSG_020001"}
    # Everything before the audit INSERT is the turn's READ path. (The post-turn
    # filter carry-over write reads the row again on purpose; those independent
    # best-effort writes are deliberately out of scope here.)
    audit_at = next(i for i, s in enumerate(statements) if "INSERT INTO query_log" in s)
    reads = [s for s in statements[:audit_at] if "FROM chat_session" in s]
    assert len(reads) == 1  # one row read per turn, not one per carry-over site
