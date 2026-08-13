"""Conversational session behavior.

Conversation memory is allowed to carry product context across follow-up turns,
but it must never become evidence and must never allow cross-drug citations.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func
from sqlmodel import col, select

from regwatch.common.conversation import PriorTurn, update_session_filters
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import turn_gate as tg
from regwatch.generate.llm import LLMResponse
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import ChatMessage, ChatSession, QueryLog
from regwatch.store.vector_store import add_chunks
from tests.conftest import synth_turn_json

# The adversarial structured turn. Claim 0 declares ONLY the retrieved albuterol
# passage, so it is admitted and the turn stays an ANSWER (which is what keeps the
# session/audit bookkeeping below observable). Claim 1 declares the valid albuterol
# pair AND a wrong-drug levalbuterol pair that was never retrieved this turn.
#
# The contract changed here and the assertions below changed with it: the old gate
# scrubbed the bad pair OUT of the bracket and let the sentence stand, re-stamping
# model text onto a passage that may not support it. The turn gate drops the WHOLE
# claim instead (OD-4) -- strictly stricter -- so claim 1's text never renders at
# all. Its wording is deliberately free of MATERIALITY_WORDS so the drop is
# immaterial and the turn renders with a disclosure; a materiality word there
# would reject the entire answer and this test would stop covering the
# leak-inside-a-surfaced-answer case.
_LEAKY_TURN = synth_turn_json(
    [
        ("The albuterol PSG evidence supports the requested point", [("PSG_020503", 4)]),
        (
            "Cross-product dissolution details appear in a second guidance document",
            [("PSG_020503", 4), ("PSG_021730", 4)],
        ),
    ]
)


class _LeakyLLM:
    name = "stub"

    def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        return LLMResponse(text=_LEAKY_TURN, model="stub")


_APPLICATION_SCOPED_TURN = synth_turn_json(
    [("The selected application recommends a fasting study", [("PSG_020911", 5)])]
)


class _ApplicationScopedLLM:
    name = "stub"

    def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        return LLMResponse(text=_APPLICATION_SCOPED_TURN, model="stub")


def _seed_two_inhalation_drugs() -> None:
    init_db()
    rows = [
        (
            "Fasting single-dose BE study for albuterol sulfate. Dissolution method details.",
            "020503",
            "albuterol sulfate",
            4,
        ),
        (
            "Fasting single-dose BE study for levalbuterol tartrate. Dissolution method details.",
            "021730",
            "levalbuterol tartrate",
            4,
        ),
    ]
    texts = [text for text, _, _, _ in rows]
    add_chunks(
        ids=[f"chunk-{appl}" for _, appl, _, _ in rows],
        embeddings=get_embedding_provider().embed(texts),
        documents=texts,
        metadatas=[
            {
                "doc_id": idx + 1,
                "version_id": idx + 10,
                "page": page,
                "normalized_name": name,
                "appl_no": appl,
                "source_url": f"https://example.invalid/PSG_{appl}.pdf",
                "section_path": "",
                "dosage_form": "Aerosol, Metered",
                "route": "Inhalation",
                "psg_type": "draft",
            }
            for idx, (_, appl, name, page) in enumerate(rows)
        ],
    )


def _seed_same_product_applications() -> None:
    """Two current PSGs with the same ingredient/form but different RLDs."""
    init_db()
    rows = [
        ("The selected RLD recommends a fasting study and dissolution testing.", "020911"),
        ("The other RLD recommends a fed study and dissolution testing.", "207921"),
    ]
    texts = [text for text, _ in rows]
    add_chunks(
        ids=[f"chunk-{appl}" for _, appl in rows],
        embeddings=get_embedding_provider().embed(texts),
        documents=texts,
        metadatas=[
            {
                "doc_id": idx + 1,
                "version_id": idx + 10,
                "page": 5,
                "normalized_name": "beclomethasone dipropionate",
                "appl_no": appl,
                "source_url": f"https://example.invalid/PSG_{appl}.pdf",
                "section_path": "",
                "dosage_form": "Aerosol, Metered",
                "route": "Inhalation",
                "psg_type": "draft",
            }
            for idx, (_, appl) in enumerate(rows)
        ],
    )


def _count(model: Any) -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_follow_up_uses_session_product_context_without_cross_drug_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_inhalation_drugs()
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _LeakyLLM())

    first = qa_mod.ask("What BE study does FDA recommend for albuterol sulfate?")
    assert first.status == "answer"
    assert first.session_id
    assert {r["short_name"] for r in first.retrieved} == {"PSG_020503"}
    assert "PSG_021730" not in first.answer

    follow_up = qa_mod.ask("What about dissolution?", session_id=first.session_id)

    assert follow_up.status == "answer"
    assert follow_up.session_id == first.session_id
    assert follow_up.turn_id != first.turn_id
    assert {r["short_name"] for r in follow_up.retrieved} == {"PSG_020503"}
    assert {(c.short_name, c.page) for c in follow_up.citations} == {("PSG_020503", 4)}
    assert "PSG_021730" not in follow_up.answer
    # The leaky claim is dropped WHOLE, not scrubbed: neither its citation nor
    # its TEXT may reach the user riding on the valid pair it also declared.
    assert "Cross-product dissolution details" not in follow_up.answer
    # OD-5: the user is told something was removed, in plain language.
    assert tg.PARTIAL_DROP_DISCLOSURE in follow_up.answer

    with session_scope() as s:
        session = s.get(ChatSession, first.session_id)
        assert session is not None
        assert dict(session.active_filters_json)["normalized_name"] == "albuterol sulfate"
        logs: list[dict[str, Any]] = [
            {
                "session_id": row.session_id,
                "turn_id": row.turn_id,
                "status": row.status,
                "route_json": dict(row.route_json),
                "retrieved_json": list(row.retrieved_json),
                "citations_json": list(row.citations_json),
            }
            for row in s.scalars(select(QueryLog).order_by(col(QueryLog.id)))
        ]
        message_roles = [
            row.role for row in s.scalars(select(ChatMessage).order_by(col(ChatMessage.created_at)))
        ]

    assert _count(QueryLog) == 2
    assert _count(ChatMessage) == 4
    assert set(message_roles) == {"user", "assistant"}
    assert all(log["session_id"] == first.session_id for log in logs)
    assert all(log["turn_id"] for log in logs)
    assert all(log["status"] == "answer" for log in logs)
    assert all(
        log["route_json"].get("filters", {}).get("normalized_name") == "albuterol sulfate"
        for log in logs
    )
    assert all(log["retrieved_json"] for log in logs)
    assert all(log["citations_json"] for log in logs)
    # OD-5's operator half: the drop the USER only sees as a plain-language
    # disclosure is fully forensic in the audit row -- which claim, why, and the
    # exact (short_name, page) that failed. A silent drop would be the same
    # invisible-degradation class the disclosure exists to prevent.
    ledgers = [log["route_json"]["turn"] for log in logs]
    assert all(led["verdict"] == tg.VERDICT_PARTIAL for led in ledgers)
    assert all(
        any(
            c["drop_reason"] == tg.DROP_UNKNOWN_CITATION and "PSG_021730,p.4" in c["bad_cites"]
            for c in led["claims"]
        )
        for led in ledgers
    )


def test_follow_up_preserves_application_scope_in_session_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-ingredient follow-up must not widen from one RLD to both PSGs."""
    _seed_same_product_applications()
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _ApplicationScopedLLM())
    filters = {
        "normalized_name": "beclomethasone dipropionate",
        "appl_no": "020911",
    }

    first = qa_mod.ask("What fasting study does FDA recommend?", filters=filters)
    assert first.status == "answer"
    assert {row["short_name"] for row in first.retrieved} == {"PSG_020911"}

    follow_up = qa_mod.ask("What about dissolution?", session_id=first.session_id)

    assert follow_up.status == "answer"
    assert {row["short_name"] for row in follow_up.retrieved} == {"PSG_020911"}
    with session_scope() as s:
        session = s.get(ChatSession, first.session_id)
        assert session is not None
        assert dict(session.active_filters_json) == filters
        audit_filters = [
            dict(row.route_json)["filters"]
            for row in s.scalars(select(QueryLog).order_by(col(QueryLog.id)))
        ]
        assistant_filters = [
            dict(row.filters_json)
            for row in s.scalars(
                select(ChatMessage)
                .where(ChatMessage.role == "assistant")
                .order_by(col(ChatMessage.created_at))
            )
        ]

    assert audit_filters == [filters, filters]
    assert assistant_filters == [filters, filters]


def test_update_session_filters_never_resurrects_deleted_sessions() -> None:
    # DELETE /sessions can land while a turn is still in flight; the turn's
    # closing filter update must not recreate the row — a recreated session
    # would be owner-less (user_id NULL) and thus adoptable by any
    # authenticated user who knows the id.
    init_db()
    update_session_filters("deleted-mid-flight", {"normalized_name": "albuterol sulfate"})
    with session_scope() as s:
        assert s.get(ChatSession, "deleted-mid-flight") is None


def test_scope_warning_is_conversational_and_audited() -> None:
    init_db()

    result = qa_mod.ask("What submission strategy should we use to file the ANDA?")

    assert result.status == "scope_warning"
    assert result.refused
    assert "I can help summarize and answer questions from FDA sources" in result.answer
    assert result.session_id
    assert result.turn_id
    with session_scope() as s:
        log = s.get(QueryLog, result.audit_id)
        assert log is not None
        assert log.status == "scope_warning"
        assert log.session_id == result.session_id
        assert log.turn_id == result.turn_id
        assert dict(log.route_json)["response_mode"] == "scope_warning"


# ---------- drill-down follow-ups ----------
#
# The conversational requirement is: read an analysis, then ask "why?", "what
# should I change?", "would this remediation resolve it?" and keep drilling in
# the same context. Two things blocked that, and they had to be fixed together:
#
#   1. _looks_like_follow_up did not recognise those phrasings, so the session
#      product was dropped and the turn hit the no_product refusal.
#   2. Nothing rewrites the query before it is embedded. Fixing (1) alone would
#      have carried the product forward and then embedded the literal string
#      "why?", which has no topical signal -- trading a no_product refusal for
#      a low_top_score one.


_ELABORATION_TURN = synth_turn_json(
    [("The albuterol PSG specifies a fasting single-dose design", [("PSG_020503", 4)])]
)


class _ElaborationLLM:
    name = "stub"

    def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        return LLMResponse(text=_ELABORATION_TURN, model="stub")


@pytest.mark.parametrize(
    "question",
    [
        "why?",
        "what should i change?",
        "tell me more",
        "explain that",
        "go on",
        "how would that work?",
        # Regression: these already worked, and must keep working.
        "would this remediation resolve it?",
        "what about dissolution?",
    ],
)
def test_drill_down_phrasings_are_recognized_as_follow_ups(question: str) -> None:
    assert qa_mod._looks_like_follow_up(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "albuterol sulfate",  # bare drug name -> vague-input clarify, not a follow-up
        "what is the dissolution method for metformin?",
        "hello",
    ],
)
def test_a_standalone_question_is_not_a_follow_up(question: str) -> None:
    assert qa_mod._looks_like_follow_up(question) is False


def test_a_contentless_follow_up_is_re_anchored_on_the_prior_question() -> None:
    """ "why?" must not be what gets embedded.

    Retrieval-only: the synthesizer still receives the user's literal words, so
    the answer addresses what was actually asked.
    """
    prior = [PriorTurn(question="What fed BE study does albuterol need?", answer="x", status="a")]

    rewritten = qa_mod._retrieval_query(
        "why?", normalized_name="albuterol sulfate", prior_turns=prior
    )

    assert rewritten == "What fed BE study does albuterol need? why?"


def test_the_rewrite_is_the_identity_when_the_question_carries_a_topic() -> None:
    """Today's working follow-ups keep their own embedding, undiluted."""
    prior = [PriorTurn(question="What fed BE study?", answer="x", status="a")]

    assert (
        qa_mod._retrieval_query(
            "what about dissolution?", normalized_name="albuterol sulfate", prior_turns=prior
        )
        == "what about dissolution?"
    )


def test_the_rewrite_is_the_identity_with_no_prior_turn() -> None:
    """Nothing to anchor on is not a licence to guess."""
    assert (
        qa_mod._retrieval_query("why?", normalized_name="albuterol sulfate", prior_turns=[])
        == "why?"
    )


def test_a_misspelled_different_product_breaks_the_follow_up_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard that makes widening the follow-up prefixes safe.

    The user changed subject to a DIFFERENT drug and merely misspelled it.
    Inheriting the session's product here would answer a levalbuterol question
    out of the albuterol guidance -- silently, with real-looking citations.
    The same hole existed on the original prefixes before this guard
    ("what about levalbuterl?" inherited albuterol outright).
    """
    _seed_two_inhalation_drugs()
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _ElaborationLLM())

    first = qa_mod.ask("What BE study does FDA recommend for albuterol sulfate?")
    assert first.status == "answer"

    switched = qa_mod.ask("what about levalbuterl tartrate?", session_id=first.session_id)

    assert switched.status == "clarify"
    assert switched.reason == "did_you_mean"
    assert switched.citations == []
    # The decisive assertion: it did NOT quietly answer out of albuterol.
    assert "PSG_020503" not in switched.answer


def test_tell_me_more_elaborates_instead_of_serving_a_clarify_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end product behaviour, and the reason the rewrite exists.

    "tell me more" is topic-less, so the vague-input gate used to intercept it
    and offer a menu to a user who had just asked to hear more about the thing
    they were already discussing.
    """
    _seed_two_inhalation_drugs()
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _ElaborationLLM())

    first = qa_mod.ask("What BE study does FDA recommend for albuterol sulfate?")
    assert first.status == "answer"

    more = qa_mod.ask("tell me more", session_id=first.session_id)

    assert more.status == "answer"
    assert more.reason != "vague_input"
    assert {(c.short_name, c.page) for c in more.citations} == {("PSG_020503", 4)}
    with session_scope() as s:
        log = s.get(QueryLog, more.audit_id)
        assert log is not None
        route = dict(log.route_json)
        assert route["context_applied"] is True
        # The audit row must show this turn searched on something other than
        # the user's words. The rewritten TEXT is deliberately not persisted.
        assert route["retrieval_query_rewritten"] is True


def test_a_vague_input_with_no_history_still_clarifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conjunct that keeps the exemption honest.

    With no prior turn there is nothing to re-anchor on, so exempting the vague
    gate would trade a useful clarify menu for a low_top_score refusal.
    """
    _seed_two_inhalation_drugs()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _ElaborationLLM())

    result = qa_mod.ask("albuterol sulfate")

    assert result.status == "clarify"
    assert result.reason == "vague_input"
