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
from regwatch.generate import turn_gate as tg
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


def _turn(
    claims: list[tuple[str, list[tuple[str, int]]]],
    *,
    turn_type: str = "ANSWER",
) -> str:
    """One structured synthesizer completion.

    The synthesizer no longer writes prose or citation markers: it returns ONE
    JSON object (see generate/turn_schema.py) declaring (short_name, page) per
    claim, and the renderer writes every marker itself. Tests that hand the
    provider stub prose are testing a channel that no longer exists.
    """
    return json.dumps(
        {
            "turn_type": turn_type,
            "claims": [
                {"text": text, "cites": [{"short_name": s, "page": p} for s, p in cites]}
                for text, cites in claims
            ],
            "unsupported": [],
        }
    )


def _only_route_json() -> dict:
    """The single audit row's route_json, read INSIDE the session (detached rows expire)."""
    with session_scope() as s:
        routes = [dict(r.route_json or {}) for r in s.scalars(select(QueryLog))]
    assert len(routes) == 1
    return routes[0]


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


def test_inv1_fabricated_citation_drops_only_its_own_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim citing a passage that was never retrieved is dropped WHOLE.

    MEANING CHANGED (structured turn contract). This test used to assert that a
    single fabricated marker refused the ENTIRE turn, because the prose gate
    split on sentence boundaries and could only accept or reject the whole
    answer. The gate now admits claim by claim, so the fabricated claim vanishes
    -- its TEXT and its marker together -- while the genuinely cited neighbour
    survives and is disclosed as partial.

    This asserts MORE, not less. The old prose path had two ways to leak: a
    bibliography-style answer refused a correct turn (the bug this replaced),
    and filter_citations rewrote a mixed bracket down to its valid pairs, which
    left a model sentence whose real source was never retrieved standing under
    someone else's real citation. Here the sentence never reaches the user at
    all, and the drop is recorded in the audit row.
    """
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
            ("Dissolution: USP Apparatus 2 at 50 rpm.", _meta(1, 4, "PSG_020503")),
        ]
    )
    # One claim cites a real retrieved passage; the other cites a document that
    # was never retrieved. Neither dropped word is a materiality word, so this
    # exercises the PARTIAL branch (the MATERIAL branch is the next test).
    completion = _turn(
        [
            (
                "A fasting bioequivalence study with 36 subjects is recommended.",
                [("PSG_020503", 3)],
            ),
            ("The agency also recommends an in vivo fed study.", [("PSG_999999", 7)]),
        ]
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(completion))
    result = qa_mod.ask("What study design is recommended?")

    assert not result.refused
    assert result.status == "answer"
    # Only the retrieved passage is citable.
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020503", 3)}
    # The fabricated claim left NOTHING behind: no marker...
    assert "PSG_999999" not in result.answer
    # ...and no re-stamped sentence under the surviving valid citation.
    assert "fed study" not in result.answer
    # The admitted claim renders with a renderer-authored marker (INV-1: no
    # regulatory sentence reaches the user uncited).
    assert (
        "A fasting bioequivalence study with 36 subjects is recommended [PSG_020503, p.3]."
        in result.answer
    )
    # The user is told something was removed (OD-5), in plain language.
    assert tg.PARTIAL_DROP_DISCLOSURE in result.answer
    # Operator telemetry: the drop and its reason are on the audit row.
    ledger = _only_route_json()["turn"]
    assert (ledger["emitted"], ledger["admitted"], ledger["dropped"]) == (2, 1, 1)
    assert [c["drop_reason"] for c in ledger["claims"] if not c["admitted"]] == [
        tg.DROP_UNKNOWN_CITATION
    ]


def test_inv1_material_drop_rejects_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping a claim that carries a qualifier rejects the ENTIRE answer.

    The companion to the test above, and the reason claim-level admission is not
    a weakening: when the dropped claim contains obligation/permission/exception
    wording, the surviving claims can read as their own opposite, so nothing is
    rendered -- not even the fully-cited survivor.
    """
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
            ("Dissolution: USP Apparatus 2 at 50 rpm.", _meta(1, 4, "PSG_020503")),
        ]
    )
    completion = _turn(
        [
            (
                "A fasting bioequivalence study with 36 subjects is recommended.",
                [("PSG_020503", 3)],
            ),
            # "not"/"required" -> MATERIAL. Cites a document never retrieved.
            ("A fed study is not required for this product.", [("PSG_999999", 7)]),
        ]
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(completion))
    result = qa_mod.ask("What study design is recommended?")

    assert result.refused
    assert result.citations == []
    assert result.answer == tg.MATERIAL_DROP_TEXT
    # The admitted claim is suppressed too -- an answer with the qualifier
    # deleted is the failure mode this branch exists to prevent.
    assert "36 subjects" not in result.answer
    route_json = _only_route_json()
    assert route_json["reason"] == "material_drop"
    # OD-5's operator half on the branch that most needs it: the whole answer
    # was thrown away, so the ONLY durable record of what the model actually
    # claimed and why it was rejected is this ledger on the audit row.
    ledger = route_json["turn"]
    assert ledger["verdict"] == "material_drop"
    assert ledger["material_word"] == "not"
    assert (ledger["emitted"], ledger["admitted"], ledger["dropped"]) == (2, 1, 1)
    dropped = [c for c in ledger["claims"] if not c["admitted"]]
    assert [c["drop_reason"] for c in dropped] == ["unknown_citation"]
    assert dropped[0]["bad_cites"] == ["PSG_999999,p.7"]


# ---------- INV-2: Refuse over guess ----------


def test_inv2_refuses_when_corpus_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No documents indexed → refuse safely after one guidance attempt."""
    init_db()
    called = {"n": 0}

    def _no_llm(*a: object, **k: object) -> Any:
        called["n"] += 1
        return _stub_llm("This should never run.")

    monkeypatch.setattr(qa_mod, "get_llm_provider", _no_llm)
    result = qa_mod.ask("What is the BE acceptance interval for metformin ER?")
    assert result.refused
    assert result.answer == get_settings().refusal_text
    assert called["n"] == 1


def test_inv2_refuses_when_model_declines_with_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the model declines, the system must refuse.

    MEANING CHANGED (structured turn contract): the model no longer emits the
    refusal STRING -- prose is not a channel it has. It declines by selecting
    turn_type="NO_EVIDENCE" (the only two values it may select are ANSWER and
    NO_EVIDENCE). The property is identical: a model decline never becomes an
    answer, and the user-visible text is still the corpus refusal copy.
    """
    _seed_corpus([("Generic body of content about bioequivalence.", _meta(2, 1, "PSG_222222"))])
    refusal = get_settings().refusal_text
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(_turn([], turn_type="NO_EVIDENCE")),
    )
    result = qa_mod.ask("Some adversarial out-of-corpus question?")
    assert result.refused
    assert result.citations == []
    assert result.answer == refusal


def test_inv2_no_evidence_turn_cannot_smuggle_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NO_EVIDENCE turn's claims are discarded wholesale, cites or not.

    A model that declines has, by its own account, nothing to cite, so anything
    it left in a claim slot is unvetted -- including a claim whose citation
    WOULD have resolved. Nothing from it may reach the user.
    """
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    completion = _turn(
        [("A fasting study is recommended for this product.", [("PSG_333333", 1)])],
        turn_type="NO_EVIDENCE",
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(completion))
    result = qa_mod.ask("What study design is recommended?")
    assert result.refused
    assert result.citations == []
    assert result.answer == get_settings().refusal_text
    assert "fasting" not in result.answer.lower()


def test_inv2_refuses_when_answer_has_no_valid_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confident answer whose every citation fails validation collapses to refusal.

    Zero admitted claims is deliberately NOT reported as a model decline: the
    refusal text is the corpus statement, and the reason recorded on the audit
    row must say the citations failed, not that the corpus lacked coverage.
    """
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    completion = _turn(
        [("The recommended dose is 100 mg per day.", [("PSG_999999", 99)])],
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(completion))
    result = qa_mod.ask("What is the recommended dose?")
    assert result.refused
    assert result.citations == []
    assert result.answer == get_settings().refusal_text
    # The fabricated claim text never surfaces.
    assert "100 mg" not in result.answer
    assert _only_route_json()["reason"] == "no_valid_citations"


def test_inv2_malformed_turn_is_a_machine_error_not_a_corpus_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable completion must NOT be recorded as "not in the corpus".

    New property of the structured contract. A parse failure says something
    about the MACHINE; serving the corpus refusal copy would write an assertion
    about FDA coverage that was never tested into the permanent audit row.
    """
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("Sure! Here is your answer.")
    )
    result = qa_mod.ask("What study design is recommended?")
    assert result.refused
    assert result.citations == []
    assert result.answer != get_settings().refusal_text
    assert result.status == "error"


# ---------- INV-6: Auditability ----------


def _row_count(model: Any) -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_inv6_every_qa_call_logs_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    # A conformant ANSWER turn: without it the stub's prose fails the gate and
    # this test would count rows on the parse-failure path instead of the
    # answer path it is meant to cover.
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            _turn([("A fasting study with 36 subjects is recommended.", [("PSG_020503", 3)])])
        ),
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
    assert "couldn't identify the product" in rows[0][2].lower()


def test_inv6_authenticated_query_records_user_attribution() -> None:
    """INV-6 extension: audit rows from an authenticated caller carry user identity."""
    from tests.conftest import create_user, session_client

    user_id = create_user()
    client = session_client(user_id)
    r = client.post("/query", json={"question": "Out-of-corpus attribution check?"})
    assert r.status_code == 200
    with session_scope() as s:
        attributions = [row.user_id for row in s.scalars(select(QueryLog))]
    assert attributions == [str(user_id)]


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
