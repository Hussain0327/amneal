"""INV-1 at the ``ask()`` boundary: what the USER is allowed to read.

tests/test_turn_gate.py pins the gate as a unit. This file pins the end of the
pipe: every rule below is stated in terms of ``QAResult.answer`` /
``QAResult.citations`` / the audit row, because that is the surface an API
client and the UI actually see.

The synthesizer no longer writes prose or citation markers. It returns ONE JSON
object (regwatch/generate/turn_schema.py); the gate admits CLAIMS one at a time
against the passages retrieved this turn, and the RENDERER writes every marker
from a validated passage. So the properties pinned here are:

  * a claim whose cites do not all resolve is dropped WHOLE -- neither its text
    nor its fabricated marker reaches the user,
  * a MATERIAL drop rejects the entire answer (a surviving fragment can read as
    its own opposite),
  * an immaterial drop is DISCLOSED to the user,
  * model-authored markers are stripped; markers in the answer are canonical and
    renderer-authored,
  * a payload that does not parse is a MACHINE error, never a claim about the
    corpus,
  * nothing model-authored escapes on a NO_EVIDENCE turn,
  * a validated answer is never returned without an audit row (INV-6).
"""

from __future__ import annotations

from typing import Any

import pytest
from config.settings import get_settings

from regwatch.common.citations import has_citation
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import turn_gate as tg
from regwatch.generate.llm import LLMResponse
from regwatch.generate.prompts import GROUNDED_QA_PROMPT
from regwatch.process.embedder import get_embedding_provider
from regwatch.retrieve.retriever import RetrievedPassage
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import QueryLog
from regwatch.store.vector_store import add_chunks
from tests.conftest import synth_turn_json

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


def _seed_two_pages() -> None:
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
            ("Dissolution: USP Apparatus 2 at 50 rpm.", _meta(1, 4, "PSG_020503")),
        ]
    )


# ---------- INV-1: an unresolvable cite drops the WHOLE claim ----------


def test_inv1_unresolvable_cite_drops_the_whole_claim_and_its_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim citing a passage that was never retrieved cannot reach the user.

    Neither half of it may survive: not the fabricated (PSG_999999, p.9) pointer
    and not the claim TEXT re-stamped onto the valid sibling's passage. The
    dropped claim here carries no obligation/permission/exception word, so the
    turn is renderable -- and the drop must be DISCLOSED (OD-5).
    """
    _seed_two_pages()
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            synth_turn_json(
                [
                    ("A fasting study is recommended", [("PSG_020503", 3)]),
                    ("A fed study is also advised", [("PSG_999999", 9)]),
                ]
            )
        ),
    )

    result = qa_mod.ask("What study design is recommended?")

    assert not result.refused
    assert result.status == "answer"
    # The fabricated pointer is gone, in every spelling.
    assert "PSG_999999" not in result.answer
    assert "p.9" not in result.answer
    # ...and so is the claim it was attached to. This is the OD-4 rule: the bad
    # PAIR is not simply deleted, leaving the sentence stamped with the other
    # (real) passage it was never sourced from.
    assert "fed study" not in result.answer
    # The supported sibling survives, with a renderer-written marker.
    assert "fasting study" in result.answer
    assert "[PSG_020503, p.3]" in result.answer
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020503", 3)}
    # OD-5: the user is told something was removed.
    assert tg.PARTIAL_DROP_DISCLOSURE in result.answer


def test_inv1_material_drop_rejects_the_entire_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the dropped claim carried a materiality word, nothing is rendered.

    "A fed study is NOT required..." dropped while "A fasting study is
    recommended" survives would hand back a confident, fully-cited answer with
    the exception deleted -- the surviving text reads as its own opposite. The
    owner's rule is whole-answer rejection with controlled copy.
    """
    _seed_two_pages()
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            synth_turn_json(
                [
                    ("A fasting study is recommended", [("PSG_020503", 3)]),
                    (
                        "A fed study is not required for the 45 mcg strength",
                        [("PSG_999999", 9)],
                    ),
                ]
            )
        ),
    )

    result = qa_mod.ask("What study design is recommended?")

    assert result.refused is True
    assert result.status == "refused"
    assert result.reason == "material_drop"
    assert result.citations == []
    assert result.answer == tg.MATERIAL_DROP_TEXT
    # Neither the retracted claim nor the ADMITTED one leaks on this path.
    assert "PSG_999999" not in result.answer
    assert "fed study" not in result.answer
    assert "fasting study" not in result.answer
    assert not has_citation(result.answer)


def test_model_authored_marker_never_reaches_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Markers in the answer are renderer-authored, never echoed from the model.

    A claim slot carrying its own ``[PSG_999999, p.9]`` must have it stripped
    before rendering, so a marker for a passage the claim never declared (and
    that was never retrieved) cannot ride into the prose beside a real stamp.
    """
    _seed_two_pages()
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            synth_turn_json(
                [
                    (
                        "A fasting study is recommended [PSG_999999, p.9]",
                        [("PSG_020503", 3)],
                    )
                ]
            )
        ),
    )

    result = qa_mod.ask("What study design is recommended?")

    assert not result.refused
    assert "PSG_999999" not in result.answer
    assert "p.9" not in result.answer
    assert "A fasting study is recommended [PSG_020503, p.3]." in result.answer
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020503", 3)}
    # Nothing was dropped, so there is nothing to disclose.
    assert tg.PARTIAL_DROP_DISCLOSURE not in result.answer


def test_case_insensitive_declared_cite_resolves_and_renders_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lowercase-declared short_name is a VALID citation, not an unknown one.

    (Rewritten from the prose-era test that asserted the model's own casing
    survived the marker filter: the model no longer writes markers. The property
    that still matters moved into ``turn_gate.allowed_passage_map`` -- a
    case-sensitive lookup would drop a genuinely grounded claim and could flip a
    correct answer into a refusal. The rendered marker takes its casing from the
    PASSAGE, so both claims carry the canonical form.)
    """
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
        ]
    )
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            synth_turn_json(
                [
                    ("The BE study is a two-way crossover", [("PSG_020503", 3)]),
                    ("The waiver criteria also apply", [("psg_020503", 3)]),
                ]
            )
        ),
    )

    result = qa_mod.ask("What study design is recommended?")

    assert not result.refused
    # Neither claim was dropped: both are rendered, each with its own marker.
    assert "The BE study is a two-way crossover [PSG_020503, p.3]." in result.answer
    assert "The waiver criteria also apply [PSG_020503, p.3]." in result.answer
    assert tg.PARTIAL_DROP_DISCLOSURE not in result.answer
    # Markers are canonical uppercase regardless of how the model spelled them.
    assert "psg_020503" not in result.answer
    # ...while the citations list stays deduped to one validated entry.
    assert [(c.short_name, c.page) for c in result.citations] == [("PSG_020503", 3)]


def test_supported_partial_answer_is_accepted_and_prompt_identity_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrieval-sufficiency disclosure + the audit row's forensic record.

    ``unsupported`` names the part of the QUESTION the passages did not answer.
    It renders through the SAME prefix the eval set pins, sets
    route_json["partial_evidence"], and the turn ledger (route_json["turn"]) has
    to record what the gate saw and decided.
    """
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            synth_turn_json(
                [("A fasting bioequivalence study is recommended", [("PSG_020503", 3)])],
                unsupported=("waiver conditions",),
            )
        ),
    )

    result = qa_mod.ask(
        "What study design and waiver conditions does the albuterol sulfate guidance state?"
    )

    assert not result.refused
    assert tg.PARTIAL_EVIDENCE_PREFIX in result.answer
    assert "waiver conditions" in result.answer
    assert "[PSG_020503, p.3]" in result.answer
    with session_scope() as session:
        row = session.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.route_json["prompt"] == GROUNDED_QA_PROMPT.as_dict()
        assert row.route_json["partial_evidence"] is True
        ledger = row.route_json["turn"]
        assert ledger["renderer_version"] == tg.RENDERER_VERSION
        assert ledger["verdict"] == tg.VERDICT_ANSWER
        assert ledger["emitted"] == 1
        assert ledger["admitted"] == 1
        assert ledger["dropped"] == 0
        assert ledger["unsupported_kept"] == ["waiver conditions"]
        assert ledger["claims"][0]["cites"] == ["PSG_020503,p.3"]


# ---------- citation binds to the best-ranked same-page chunk ----------


def test_citation_binds_best_ranked_same_page_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When several retrieved chunks share a (doc, page), the citation the CALLER
    receives must carry the TOP-ranked chunk's id/snippet/score (passages arrive
    best-first) -- not the weakest one that happened to be listed last.

    (The property used to live in ``grounded_qa._validate_citations``; it now
    lives in ``turn_gate.allowed_passage_map``. Pinned here end-to-end, through
    ``ask()``, because the evidence drawer renders exactly this snippet/score.)
    """
    _seed_corpus([("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503"))])

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
    monkeypatch.setattr(qa_mod, "retrieve", lambda *a, **k: [best, worse])
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            synth_turn_json([("The dissolution method is the USP paddle", [("PSG_020503", 3)])])
        ),
    )

    result = qa_mod.ask("What dissolution method is recommended for albuterol sulfate?")

    assert not result.refused
    assert [c.chunk_id for c in result.citations] == ["chunk-best"]
    assert result.citations[0].score == 0.71
    assert "USP paddle" in result.citations[0].snippet


# ---------- a payload that does not parse is a MACHINE fault ----------


def test_prose_completion_is_a_service_error_not_a_corpus_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthesizer contract is structured JSON; prose does not parse.

    The distinction is load-bearing and is why this file no longer asserts
    ``refusal_text`` here: serving "I couldn't find this in the current FDA
    guidance corpus" would record, in the audit row forever, a claim about
    COVERAGE that was never tested. A parse failure is a statement about the
    machine, so it gets status="error" and the service-unavailable copy.
    """
    _seed_two_pages()
    prose = (
        "A fasting study is recommended [PSG_020503, p.3]. "
        "A fed study is also advised [PSG_999999, p.9]."
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(prose))

    result = qa_mod.ask("What study design is recommended?")

    assert result.refused is True
    assert result.status == "error"
    assert result.reason == "malformed_structure"
    assert result.citations == []
    assert "temporarily unavailable" in result.answer
    assert result.answer != get_settings().refusal_text
    # None of the unvetted draft escapes -- not the fabricated marker, and not
    # the marker that would have validated.
    assert "PSG_999999" not in result.answer
    assert "PSG_020503" not in result.answer
    assert not has_citation(result.answer)


def test_no_evidence_turn_leaks_no_model_authored_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that declines has, by its own account, nothing to cite.

    Anything it still put in a claim slot is unvetted by definition, so no byte
    of it may reach the user -- the turn is answered with application-authored
    copy and zero citations. (The product resolved BY NAME, so the decline
    routes to the clarify branch; the leak rule is identical either way.)
    """
    _seed_two_pages()
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            synth_turn_json(
                [("A fed study is required for every strength", [("PSG_020503", 3)])],
                turn_type="NO_EVIDENCE",
            )
        ),
    )

    result = qa_mod.ask("What study design does the albuterol sulfate guidance recommend?")

    assert result.citations == []
    assert result.reason == "model_refusal"
    assert "fed study" not in result.answer
    assert not has_citation(result.answer)


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
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _stub_llm(
            synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])])
        ),
    )

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
    assert not has_citation(result.answer)
    # The refusal's own audit write also failed -> skipped, sentinel id.
    assert result.audit_id == -1


def test_rendered_answer_never_streams_a_retracted_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token sink replays the GATED answer, never provisional model tokens.

    Live token streaming from inside synthesis was removed with the prose gate:
    a user could otherwise read text the gate later retracted. What the sink
    emits must reassemble to exactly ``result.answer`` -- and on a rejected turn
    it must emit nothing at all.
    """
    _seed_two_pages()

    def _run(raw: str) -> tuple[Any, str]:
        chunks: list[str] = []
        monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(raw))
        res = qa_mod.ask("What study design is recommended?", on_token=chunks.append)
        return res, "".join(chunks)

    good, streamed = _run(
        synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])])
    )
    assert not good.refused
    assert streamed == good.answer

    rejected, streamed_rejected = _run(
        synth_turn_json(
            [
                ("A fasting study is recommended", [("PSG_020503", 3)]),
                ("A fed study is not required", [("PSG_999999", 9)]),
            ]
        )
    )
    assert rejected.refused is True
    assert streamed_rejected == ""
