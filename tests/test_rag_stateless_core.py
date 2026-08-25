"""Step 2 of the strangler migration: the RAG core is stateless.

``ask_core`` performs NO persistence on any path -- it returns (RagOutcome,
AuditPayload, SessionPatch) and the ``ask()`` shell owns every write (audit
row first, then the assistant message that references the audit id). Four
pinned properties:

  * core purity   -- every write function raises if touched while ask_core
                     runs (success, refusal, clarify, and meta paths alike),
                     and the core emits no user-visible bytes at all: it has
                     no token sink any more, so nothing can reach a reader
                     before the gate and the audit write have both run;
  * shell ordering -- ask() writes the user message, THEN the audit row, THEN
                     the assistant message carrying that audit id (INV-6:
                     the audit write precedes everything the turn leaves
                     behind except the pre-compute user row), and the
                     ``on_token`` replay comes after the audit row and only
                     on a turn that actually answered;
  * shell parity  -- ask() persists exactly what the pre-refactor code wrote,
                     for the answer flow AND each terminal decline family
                     (audit row fields, both chat messages, filter carry-over);
  * payload completeness -- every exit status yields an AuditPayload the shell
                     can log verbatim (INV-6 on every branch) and all three
                     contract objects stay asdict/JSON-serializable (they are
                     designed to become a cross-service HTTP contract).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest
from sqlmodel import col, select

from regwatch.common.audit import log_query
from regwatch.common.conversation import open_turn, record_message, update_session_filters
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.rag_contract import AuditPayload, RagOutcome, SessionPatch
from regwatch.process.embedder import get_embedding_provider
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import ChatMessage, ChatSession, QueryLog
from regwatch.store.vector_store import add_chunks
from tests.conftest import synth_turn_json
from tests.test_invariants import _meta, _seed_corpus, _stub_llm

pytestmark = pytest.mark.invariants

# Everything in grounded_qa that persists (writes) plus the module-level session
# READ seam: the core must reach session context ONLY through the injected
# loaders, so touching any of these from inside ask_core is a purity breach.
# `open_turn` is the shell's opening transaction (session upsert + user message
# + both reads) and `read_turn_context` is the native path's combined read; the
# two per-helper readers no longer exist in this module's namespace.
_FORBIDDEN_IN_CORE = (
    "log_query",
    "_log_query_or_skip",
    "open_turn",
    "record_message",
    "update_session_filters",
    "read_turn_context",
)


def _forbid_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _FORBIDDEN_IN_CORE:

        def _boom(*args: Any, _name: str = name, **kwargs: Any) -> Any:
            raise AssertionError(f"stateless ask_core touched {_name}")

        monkeypatch.setattr(qa_mod, name, _boom)


def _run_core(question: str) -> tuple[RagOutcome, AuditPayload, SessionPatch]:
    return qa_mod.ask_core(
        question,
        session_id="sess-core",
        turn_id="turn-core",
        load_session_filters=lambda: {},
        load_recent_turns=lambda: [],
    )


def _seed_albuterol() -> None:
    _seed_corpus(
        [
            ("Fasting bioequivalence study with 36 subjects.", _meta(1, 3, "PSG_020503")),
            ("Dissolution: USP Apparatus 2 at 50 rpm.", _meta(1, 4, "PSG_020503")),
        ]
    )


def _seed_names(names: list[str]) -> None:
    """One chunk per drug name (mirrors tests/test_clarify.py) -- a multi-product
    corpus so a no-drug meta question resolves to none and the meta gate fires."""
    init_db()
    embedder = get_embedding_provider()
    texts = [f"Bioequivalence study guidance for {n}." for n in names]
    add_chunks(
        ids=[f"chunk-{i}" for i in range(len(names))],
        embeddings=embedder.embed(texts),
        documents=texts,
        metadatas=[
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
        ],
    )


# The synthesizer returns a STRUCTURED turn, never prose: one claim, one
# (short_name, page) pair that the seeded corpus actually contains. The claim
# text carries no citation marker of its own -- markers are renderer-authored,
# so a stub that wrote one would be testing a channel the gate strips.
_CLAIM_TEXT = "A fasting study is recommended"
_CITED_TURN = synth_turn_json([(_CLAIM_TEXT, [("PSG_020503", 3)])])
# What the renderer stamps onto that claim. Asserted (rather than recomputed
# from the module under test) so a silent change to marker shape shows up here
# as a persistence-parity failure instead of passing vacuously.
_CLAIM_MARKER = "[PSG_020503, p.3]"


# ---------- core purity: no writes on any path ----------


def test_core_success_path_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_albuterol()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_CITED_TURN))
    _forbid_persistence(monkeypatch)

    outcome, audit, patch = _run_core("What study design is recommended?")

    assert outcome.status == "answer"
    assert not outcome.refused
    assert [(c.short_name, c.page) for c in outcome.citations] == [("PSG_020503", 3)]
    assert outcome.session_id == "sess-core"
    assert outcome.turn_id == "turn-core"
    # The validated-answer audit is STRICT (no-audit-no-answer, INV-6) and
    # carries the degrade turn the shell serves if the audit write fails.
    assert audit.allow_skip is False
    assert audit.failure_fallback is not None
    fb_outcome, fb_audit, _fb_patch = audit.failure_fallback
    assert fb_outcome.status == "error"
    assert fb_outcome.refused is True
    assert "temporarily unavailable" in fb_outcome.answer
    assert fb_audit.allow_skip is True  # the fallback's own audit may skip to -1
    # The patch is a description, not an applied write.
    assert patch.content == outcome.answer
    assert patch.update_filters is True
    assert patch.filters["normalized_name"] == "albuterol sulfate"


def test_core_refusal_path_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()  # nothing indexed: an empty corpus is a real evidence gap
    _forbid_persistence(monkeypatch)

    outcome, audit, patch = _run_core("What does the FDA recommend for romidepsin?")

    assert outcome.refused is True
    assert outcome.status == "refused"
    # An EMPTY CORPUS refuses. "Which product did you mean?" would be a lie
    # here: no product would work. Distinct from an unresolved product against
    # a populated corpus, which converses (see test_clarify).
    assert outcome.reason == "empty_corpus"
    assert outcome.citations == []
    assert audit.refused is True
    assert audit.allow_skip is True
    assert audit.failure_fallback is None
    assert patch.update_filters is False


def test_core_clarify_path_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_albuterol()
    _forbid_persistence(monkeypatch)

    outcome, audit, patch = _run_core("albuterol sulfate")

    assert outcome.status == "clarify"
    assert not outcome.refused
    assert outcome.clarify  # offered options, never a guess
    assert outcome.citations == []  # never fabricates
    assert audit.status == "clarify"
    assert audit.allow_skip is True
    assert patch.content == outcome.interpretation


def test_core_meta_path_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_names(["atorvastatin calcium", "metformin hydrochloride"])
    _forbid_persistence(monkeypatch)

    outcome, audit, patch = _run_core("what can I ask about?")

    assert outcome.status == "meta"
    assert outcome.refused is False
    assert outcome.citations == []  # system-state answer, never evidence
    assert audit.status == "meta"
    assert audit.allow_skip is True
    assert patch.update_filters is False


# ---------- shell ordering: audit row before the assistant message ----------


def test_shell_write_order_audit_before_assistant_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_albuterol()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_CITED_TURN))

    # The real write functions, taken from their home modules (identical
    # objects to grounded_qa's module globals before the monkeypatch below).
    events: list[tuple[str, int | None]] = []

    def _rec_log(**kw: Any) -> int:
        audit_id = log_query(**kw)
        events.append(("log_query", audit_id))
        return audit_id

    def _rec_open(**kw: Any) -> Any:
        # The user row is written inside open_turn's single opening
        # transaction now, not through record_message, so the ordering probe
        # sits on that seam instead.
        events.append(("message:user", None))
        return open_turn(**kw)

    def _rec_msg(**kw: Any) -> str:
        events.append((f"message:{kw['role']}", kw.get("audit_id")))
        return record_message(**kw)

    def _rec_update(session_id: str | None, filters: dict[str, Any] | None) -> None:
        events.append(("update_filters", None))
        update_session_filters(session_id, filters)

    monkeypatch.setattr(qa_mod, "log_query", _rec_log)
    monkeypatch.setattr(qa_mod, "open_turn", _rec_open)
    monkeypatch.setattr(qa_mod, "record_message", _rec_msg)
    monkeypatch.setattr(qa_mod, "update_session_filters", _rec_update)

    result = qa_mod.ask("What study design is recommended?")

    assert result.status == "answer"
    assert [kind for kind, _ in events] == [
        "message:user",
        "log_query",
        "message:assistant",
        "update_filters",
    ]
    # The user row predates the audit; the assistant row references it.
    assert events[0][1] is None
    assert events[1][1] == result.audit_id
    assert events[2][1] == result.audit_id


def test_shell_replays_tokens_only_after_the_audit_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_token`` is a SHELL concern now, not a core one.

    The core used to stream provisional model tokens, so a reader could see text
    the citation gate later retracted, and a complete answer could reach them
    with no audit row anywhere. The sink now replays the rendered, gated answer
    after the audit row commits. Two properties, both INV-1/INV-6 load-bearing:
    every byte emitted is exactly the recorded answer, and no byte precedes the
    audit write.
    """
    _seed_albuterol()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_CITED_TURN))

    events: list[str] = []

    def _rec_log(**kw: Any) -> int:
        events.append("log_query")
        return log_query(**kw)

    monkeypatch.setattr(qa_mod, "log_query", _rec_log)

    tokens: list[str] = []

    def _on_token(delta: str) -> None:
        events.append("token")
        tokens.append(delta)

    result = qa_mod.ask("What study design is recommended?", on_token=_on_token)

    assert result.status == "answer"
    assert tokens  # an answer turn does emit
    # Byte-identical to the record: the sink cannot show a draft, a stripped
    # marker, or a claim the gate dropped.
    assert "".join(tokens) == result.answer
    assert events.count("log_query") == 1
    assert events.index("log_query") < events.index("token")


# ---------- shell parity: ask() persists the pre-refactor golden ----------


def test_shell_persists_the_pre_refactor_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_albuterol()
    question = "What study design is recommended?"
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_CITED_TURN))

    result = qa_mod.ask(question)

    assert result.status == "answer"
    assert not result.refused
    assert result.audit_id > 0
    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.mode == "qa"
        assert row.query_text == question
        assert row.refused is False
        assert row.status == "answer"
        assert row.session_id == result.session_id
        assert row.turn_id == result.turn_id
        assert row.answer_text == result.answer
        # What is persisted is the RENDERED answer, not the model's draft: the
        # claim text plus a renderer-authored marker. The model never wrote one.
        assert row.answer_text.startswith(f"{_CLAIM_TEXT} {_CLAIM_MARKER}.")
        assert [(c["short_name"], c["page"]) for c in row.citations_json] == [("PSG_020503", 3)]
        assert list(row.retrieved_json) == result.retrieved
        route = dict(row.route_json)
        assert route["reason"] == "retrieval"
        assert route["response_mode"] == "answer"
        assert route["filters"]["normalized_name"] == "albuterol sulfate"
        # The gate's forensic ledger rides on the SAME audit row (its id is the
        # response id), so a drop decision is reconstructable after the fact.
        turn_ledger = dict(route["turn"])
        assert turn_ledger["verdict"] == "answer"
        assert turn_ledger["emitted"] == 1
        assert turn_ledger["admitted"] == 1
        assert turn_ledger["dropped"] == 0
        assert [c["index"] for c in turn_ledger["claims"]] == [0]
        # Token fields NULL: the echo-stub response reported no usage.
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.cost_usd is None

        messages = [
            (m.role, m.content, m.status, m.model_name, m.audit_id, dict(m.metadata_json or {}))
            for m in s.scalars(select(ChatMessage).order_by(col(ChatMessage.created_at)))
        ]
        session = s.get(ChatSession, result.session_id)
        assert session is not None
        session_filters = dict(session.active_filters_json or {})

    assert [m[0] for m in messages] == ["user", "assistant"]
    _role, u_content, _status, _model, u_audit, _meta_json = messages[0]
    assert u_content == question
    assert u_audit is None  # the user row predates the audit write
    _role, a_content, a_status, a_model, a_audit, a_meta = messages[1]
    assert a_content == result.answer
    assert a_status == "answer"
    assert a_model == "stub"
    # The shell injected the audit id AFTER logging: the assistant message
    # references the same audit row the result carries.
    assert a_audit == result.audit_id
    assert a_meta["route"]["reason"] == "retrieval"
    assert a_meta["retrieved"] == result.retrieved
    # Filter carry-over persisted for the next turn.
    assert session_filters["normalized_name"] == "albuterol sulfate"


# ---------- AuditPayload completeness: every exit status is loggable ----------


class _BoomProvider:
    name = "boom"

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated provider outage")


def _setup_answer(monkeypatch: pytest.MonkeyPatch) -> str:
    _seed_albuterol()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_CITED_TURN))
    return "What study design is recommended?"


def _setup_summary(monkeypatch: pytest.MonkeyPatch) -> str:
    _seed_albuterol()
    # The paraphrased summary wording scores lower against the echo embedder
    # than the question-form used elsewhere; the threshold is not what this
    # scenario pins (status plumbing is), so let retrieval pass.
    monkeypatch.setenv("REFUSAL_SCORE_THRESHOLD", "0.0")
    import config.settings as cs

    cs.get_settings.cache_clear()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_CITED_TURN))
    return "Summarize the recommended study design."


def _setup_clarify(monkeypatch: pytest.MonkeyPatch) -> str:
    _seed_albuterol()
    return "albuterol sulfate"


def _setup_scope_warning(monkeypatch: pytest.MonkeyPatch) -> str:
    init_db()
    return "What submission strategy should we use to file the ANDA?"


def _setup_meta(monkeypatch: pytest.MonkeyPatch) -> str:
    _seed_names(["atorvastatin calcium", "metformin hydrochloride"])
    return "what can I ask about?"


def _setup_refused(monkeypatch: pytest.MonkeyPatch) -> str:
    init_db()
    return "What does the FDA recommend for romidepsin?"


def _setup_error(monkeypatch: pytest.MonkeyPatch) -> str:
    _seed_albuterol()
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _BoomProvider())
    return "What study design is recommended?"


_SCENARIOS = {
    "answer": _setup_answer,
    "summary": _setup_summary,
    "clarify": _setup_clarify,
    "scope_warning": _setup_scope_warning,
    "meta": _setup_meta,
    "refused": _setup_refused,
    "error": _setup_error,
}


@pytest.mark.parametrize("expected_status", sorted(_SCENARIOS))
def test_every_exit_status_yields_a_loggable_audit_payload(
    expected_status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = _SCENARIOS[expected_status](monkeypatch)

    outcome, audit, patch = _run_core(question)

    assert outcome.status == expected_status
    assert audit.status == expected_status
    assert audit.query_text == question
    assert audit.session_id == "sess-core"
    assert audit.turn_id == "turn-core"
    # The whole contract stays JSON-serializable: it is designed to cross an
    # HTTP boundary once the control plane owns the writes.
    json.dumps([asdict(outcome), asdict(audit), asdict(patch)])
    # And the shell can log it verbatim -- INV-6 holds on this branch.
    audit_id = log_query(**audit.log_kwargs())
    assert audit_id > 0
    with session_scope() as s:
        row = s.get(QueryLog, audit_id)
        assert row is not None
        assert row.status == expected_status
        assert row.refused is outcome.refused
        assert row.query_text == question


# ---------- shell parity for the terminal decline families ----------


@pytest.mark.parametrize("expected_status", ["refused", "clarify", "meta"])
def test_shell_terminal_shapes_persist_like_pre_refactor(
    expected_status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each decline family still leaves the pre-refactor trail: one audit row
    (skip-tolerant, INV-6) and an assistant message that references it."""
    question = _SCENARIOS[expected_status](monkeypatch)

    result = qa_mod.ask(question)

    assert result.status == expected_status
    assert result.refused is (expected_status == "refused")
    assert result.citations == []  # declines never carry evidence
    assert result.audit_id > 0
    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.status == expected_status
        assert row.refused is result.refused
        assert row.answer_text == result.answer
        messages = [
            (m.role, m.audit_id, m.status)
            for m in s.scalars(select(ChatMessage).order_by(col(ChatMessage.created_at)))
        ]
    assert [m[0] for m in messages] == ["user", "assistant"]
    assert messages[1][1] == result.audit_id
    assert messages[1][2] == expected_status


@pytest.mark.parametrize("expected_status", ["refused", "clarify", "meta", "error"])
def test_shell_replays_no_tokens_on_a_non_answer_turn(
    expected_status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that did not answer paints NOTHING.

    This is where the deleted ``_stream_synthesis`` sentinel guard now lives: it
    used to hold the refusal string token by token so a decline was never
    painted as a grounded answer for a beat. The shell reaches the same end by
    replaying only on an answer/summary turn -- a refusal, a clarify, a meta
    reply and a provider outage all emit zero bytes to the sink.
    """
    question = _SCENARIOS[expected_status](monkeypatch)
    tokens: list[str] = []

    result = qa_mod.ask(question, on_token=tokens.append)

    assert result.status == expected_status
    assert tokens == []
