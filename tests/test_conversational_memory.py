"""Multi-turn conversational memory — ergonomic follow-ups WITHOUT an INV-1 leak.

The synthesizer now sees the last few ANSWERED turns as a labeled, citation-free
"Recent conversation" block, so a follow-up ("what about the fed study?") resolves
naturally. These tests pin the load-bearing property: prior-turn text is context
only, never a source. A fact that lived solely in a prior turn cannot acquire a
valid citation this turn -- its claim is dropped whole and the answer refuses.

The synthesizer returns a structured turn (see generate/turn_schema.py), so the
stubs here declare (short_name, page) per claim instead of writing prose with
markers in it. The INV-1 property under memory is unchanged and is asserted
against the SAME adversarial input: a claim that re-cites a prior turn's page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from config.settings import get_settings
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from regwatch.common import conversation as conv
from regwatch.common.citations import has_citation
from regwatch.common.conversation import PriorTurn, get_recent_turns
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.store.db import get_engine, init_db, session_scope
from regwatch.store.models import ChatMessage, ChatSession, QueryLog
from tests.conftest import synth_turn_json
from tests.test_invariants import _meta, _seed_corpus, _stub_llm

pytestmark = pytest.mark.invariants


def _seed_turn(
    session_id: str,
    *,
    q: str,
    a: str,
    status: str | None = "answer",
    order: int,
    filters: dict[str, object] | None = None,
    audit_id: int | None = None,
    metadata: dict[str, object] | None = None,
) -> str:
    """Insert a completed (user, assistant) turn with explicit, ordered timestamps."""
    tid = f"turn-{order}"
    base = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=order)
    with session_scope() as s:
        s.add(
            ChatMessage(
                id=str(uuid4()),
                session_id=session_id,
                turn_id=tid,
                role="user",
                content=q,
                created_at=base,
            )
        )
        s.add(
            ChatMessage(
                id=str(uuid4()),
                session_id=session_id,
                turn_id=tid,
                role="assistant",
                content=a,
                status=status,
                filters_json=filters or {},
                audit_id=audit_id,
                metadata_json=metadata or {},
                created_at=base + timedelta(seconds=1),
            )
        )
    return tid


# The one grounded turn every prompt-shaping test hands back: one claim citing
# the single seeded passage (PSG_020503 p.3), so the turn is fully ADMITTED and
# the pipeline runs end to end instead of stopping at a parse failure.
_GROUNDED_TURN = synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])])


class _CapturingLLM:
    """Stub synthesizer that records the user prompt it was handed."""

    name = "capture"

    def __init__(self, text: str) -> None:
        self.text = text
        self.user_prompts: list[str] = []

    def complete(self, messages: list, *a: object, **kw: object) -> LLMResponse:
        self.user_prompts.append(next((m.content for m in messages if m.role == "user"), ""))
        return LLMResponse(text=self.text, model="capture")


# ---------- ensure_session origin (issue #208) ----------


def test_ensure_session_defaults_to_thread_origin() -> None:
    init_db()
    sid = conv.ensure_session()
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        assert row is not None and row.origin == "thread"


def test_ensure_session_persists_assistant_origin_on_create() -> None:
    init_db()
    sid = conv.ensure_session(origin="assistant")
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        assert row is not None and row.origin == "assistant"


def test_ensure_session_does_not_overwrite_origin_on_existing_row() -> None:
    """origin is a create-only decision, same rule active_filters_json follows:
    an established conversation does not change kind because a later caller
    (e.g. the Assistant panel replaying onto an id it does not own) asks with
    a different origin."""
    init_db()
    sid = "sess-origin-sticky"
    conv.ensure_session(sid, origin="assistant")
    conv.ensure_session(sid, origin="thread")
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        assert row is not None and row.origin == "assistant"


def test_ensure_session_rejects_unknown_origin() -> None:
    """The API boundary (QueryRequest) already validates origin; reaching here
    with junk is a programming error and must fail loudly, before any write.

    Pins the SPECIFIC type, not just ValueError: ask()'s session block catches
    SessionOriginError by that type so the record_message write it also wraps
    keeps its generic degrade path. Widening this back to a bare ValueError
    would silently re-narrow that degrade.
    """
    init_db()
    with pytest.raises(conv.SessionOriginError):
        conv.ensure_session("sess-bad-origin", origin="bogus")
    # Still a ValueError, so callers written against the broader type work.
    assert issubclass(conv.SessionOriginError, ValueError)
    with session_scope() as s:
        assert s.get(ChatSession, "sess-bad-origin") is None  # nothing was written


def test_ask_propagates_a_bad_origin_instead_of_degrading() -> None:
    """ask() degrades to a fresh session id when session bookkeeping fails, but
    a bad origin is a caller bug, not a DB hiccup -- it must surface."""
    from regwatch.generate.grounded_qa import ask

    init_db()
    with pytest.raises(conv.SessionOriginError):
        ask("Does this reach the pipeline?", origin="bogus")


def test_chat_session_db_check_rejects_unknown_origin() -> None:
    """The CHECK constraint is real, not just an application-level guard: a raw
    row that bypasses ensure_session's validation is still rejected by the DB."""
    init_db()
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(ChatSession(id="sess-db-check-origin", origin="bogus"))


# ---------- get_recent_turns unit behavior ----------


def test_get_recent_turns_pairs_orders_and_limits() -> None:
    init_db()
    sid = "sess-order"
    conv.ensure_session(sid)
    for i in range(1, 5):
        _seed_turn(sid, q=f"Q{i}", a=f"A{i}", order=i)
    turns = get_recent_turns(sid, limit=3)
    # The last 3 completed turns, oldest-first.
    assert [t.question for t in turns] == ["Q2", "Q3", "Q4"]
    assert [t.answer for t in turns] == ["A2", "A3", "A4"]


def test_get_recent_turns_excludes_current_and_non_answers() -> None:
    init_db()
    sid = "sess-filter"
    conv.ensure_session(sid)
    _seed_turn(sid, q="Q1", a="A1", status="answer", order=1)
    _seed_turn(sid, q="Q2", a="declined", status="refused", order=2)
    _seed_turn(sid, q="Q3", a="C3", status="clarify", order=3)
    _seed_turn(sid, q="Q4", a="A4", status="summary", order=4)
    current = _seed_turn(sid, q="CURRENT", a="unused", status="answer", order=5)
    turns = get_recent_turns(sid, limit=9, exclude_turn_id=current)
    # refused + clarify dropped; current excluded; answer + summary kept, oldest-first.
    assert [t.question for t in turns] == ["Q1", "Q4"]


def test_recent_product_scope_label_requires_persisted_filter_and_audit() -> None:
    init_db()
    sid = "sess-scope-label"
    conv.ensure_session(sid)
    _seed_turn(
        sid,
        q="What does the beclomethasone guidance require?",
        a="A cited answer.",
        order=1,
        filters={"normalized_name": "beclomethasone dipropionate"},
        audit_id=731,
    )
    _seed_turn(
        sid,
        q="A skip-audited answer",
        a="No durable audit row.",
        order=2,
        filters={"normalized_name": "albuterol sulfate"},
        audit_id=-1,
    )

    turns = get_recent_turns(sid, limit=3)

    assert turns[0].scope_kind == "product"
    assert turns[0].scope_audited is True
    assert turns[0].audit_id == 731
    assert turns[1].scope_kind == "none"
    assert turns[1].scope_audited is False
    assert turns[1].audit_id is None


def test_shadow_corpus_guess_never_becomes_inheritable_history() -> None:
    init_db()
    sid = "sess-shadow-corpus-label"
    conv.ensure_session(sid)
    _seed_turn(
        sid,
        q="Across the inhalation guidances, define ISM",
        a="The current deterministic no-product response.",
        status="answer",
        order=1,
        audit_id=812,
        metadata={
            "route": {
                "route_call": {
                    "outcome": "success",
                    "compiled_scope": {
                        "kind": "corpus",
                        "corpus_policy": "inhalation_psg",
                    },
                }
            }
        },
    )

    turn = get_recent_turns(sid, limit=1)[0]

    # PR11b reads only executed, application-owned product filters. It never
    # promotes advisory metadata into an audited scope. PR12 must add a
    # distinct executed-corpus ledger before corpus inheritance can exist.
    assert turn.scope_kind == "none"
    assert turn.scope_audited is False
    assert turn.corpus_policy is None
    assert turn.audit_id is None


def test_get_recent_turns_empty_and_bad_inputs() -> None:
    init_db()
    assert get_recent_turns(None) == []
    assert get_recent_turns("does-not-exist") == []
    conv.ensure_session("sess-empty")
    assert get_recent_turns("sess-empty", limit=0) == []


def test_get_session_filters_degrades_to_empty_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session filters are best-effort context, like get_recent_turns: a DB
    hiccup degrades to {} instead of raising a 500 into an answerable turn."""

    def _boom() -> object:
        raise RuntimeError("simulated db outage")

    monkeypatch.setattr(conv, "session_scope", _boom)
    assert conv.get_session_filters("sess-any") == {}


def test_get_recent_turns_degrades_to_empty_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB error degrades to no-memory rather than failing the turn -- and is
    LOGGED: a silent swallow would hide a broken DB behind subtly context-less
    answers (the recurring silent-failure incident class)."""

    class _LogRecorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def warning(self, event: str, **kwargs: object) -> None:
            self.events.append(event)

    def boom() -> object:
        raise RuntimeError("db down")

    recorder = _LogRecorder()
    monkeypatch.setattr(conv, "session_scope", boom)
    monkeypatch.setattr(conv, "log", recorder)

    assert get_recent_turns("sess-any", limit=3) == []
    assert "get_recent_turns_failed" in recorder.events


# ---------- open_turn: the whole turn opened in one transaction ----------


def test_open_turn_matches_the_legacy_readers() -> None:
    """Both pre-loaded reads mean exactly what the per-helper readers meant.

    Pins the recent-turn window semantics, oldest-first order, the
    answer/summary-only rule, _safe_filters key filtering and -- critically --
    that the user message open_turn itself just wrote is excluded from its own
    context.
    """
    init_db()
    sid = "sess-open-parity"
    conv.ensure_session(sid)
    conv.update_session_filters(sid, {"normalized_name": "estradiol", "dosage_form": "Gel"})
    _seed_turn(sid, q="Q1", a="A1", status="answer", order=1)
    _seed_turn(sid, q="Q2", a="declined", status="refused", order=2)
    _seed_turn(sid, q="Q3", a="C3", status="clarify", order=3)
    _seed_turn(sid, q="Q4", a="A4", status="summary", order=4)

    ctx = conv.open_turn(question="What about dissolution?", session_id=sid)

    assert ctx.degraded is False
    assert ctx.session_id == sid
    assert ctx.filters == {"normalized_name": "estradiol", "dosage_form": "Gel"}
    assert ctx.filters == conv.get_session_filters(sid)
    assert ctx.recent_turns == get_recent_turns(sid, limit=3, exclude_turn_id=ctx.turn_id)
    # refused + clarify dropped, this turn's own user row excluded, oldest-first.
    assert [t.question for t in ctx.recent_turns] == ["Q1", "Q4"]


def test_open_turn_commits_the_user_message_before_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user question is COMMITTED (not merely flushed) before the core runs.

    So a core exception still leaves the question in the chat history and
    /sessions shows it while the answer is still streaming. The probe reads in
    its OWN transaction, which can only see the row if the opening scope had
    already committed.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_GROUNDED_TURN))
    question = "What study design is recommended?"
    real_core = qa_mod.ask_core
    seen: dict[str, bool] = {}

    def _probe(*args: Any, **kwargs: Any) -> Any:
        with session_scope() as s:
            rows = list(s.exec(select(ChatMessage).where(ChatMessage.role == "user")))
            seen["visible"] = any(m.content == question for m in rows)
        return real_core(*args, **kwargs)

    monkeypatch.setattr(qa_mod, "ask_core", _probe)

    result = qa_mod.ask(question)

    assert seen["visible"] is True
    assert result.status == "answer"


def test_open_turn_degrades_to_a_fresh_session_and_empty_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-6: a DB hiccup in bookkeeping never stops the query being answered.

    Same degrade the shell used to own, moved verbatim into open_turn: a FRESH
    id (never the requested one, which may belong to someone else), no context,
    and the SAME `session_setup_failed` event name so nothing operational
    breaks.
    """

    class _LogRecorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def warning(self, event_name: str, **kwargs: object) -> None:
            self.events.append(event_name)

    def boom() -> object:
        raise RuntimeError("db down")

    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_GROUNDED_TURN))
    recorder = _LogRecorder()
    monkeypatch.setattr(conv, "session_scope", boom)
    monkeypatch.setattr(conv, "log", recorder)

    ctx = conv.open_turn(question="Anything at all?", session_id="sess-degrade")

    assert ctx.degraded is True
    assert ctx.session_id == ctx.turn_id != "sess-degrade"
    assert ctx.filters == {}
    assert ctx.recent_turns == []
    assert "session_setup_failed" in recorder.events

    # With that same outage in place the turn still answers and still audits.
    result = qa_mod.ask("What study design is recommended?")
    assert result.status == "answer"
    with session_scope() as s:
        assert len(list(s.exec(select(QueryLog)))) == 1


def test_open_turn_propagates_ownership_and_origin_errors() -> None:
    """The two exceptions that must NOT degrade into a fresh id.

    An ownership race lost after the API's pre-check aborts with nothing
    written; a bad origin is a caller bug caught before any I/O at all.
    """
    init_db()
    sid = "sess-open-owned"
    conv.ensure_session(sid, user_id="user-1")

    with pytest.raises(conv.SessionOwnershipError):
        conv.open_turn(
            question="Whose session is this?",
            session_id=sid,
            turn_id="turn-lost-race",
            user_id="user-2",
        )
    with session_scope() as s:
        rows = list(s.exec(select(ChatMessage).where(ChatMessage.turn_id == "turn-lost-race")))
        assert rows == []

    statements: list[str] = []

    def _capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        with pytest.raises(conv.SessionOriginError):
            conv.open_turn(question="Anything at all?", origin="bogus")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    assert statements == []  # origin is validated before the scope opens


def test_open_turn_history_failure_does_not_roll_back_the_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory read is best-effort; it cannot cost the turn its own writes.

    Folding `get_recent_turns` into the write transaction widened its blast
    radius: an ABORTED history statement (statement_timeout, an admin cancel, a
    serialization failure) would take the session upsert and the user message
    with it, orphaning the turn out of its thread. The savepoint is what keeps
    the one-transaction win and the documented memory contract at once, so this
    aborts the transaction for real -- a Python-only raise would pass with no
    savepoint at all.
    """

    class _LogRecorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def warning(self, event_name: str, **kwargs: object) -> None:
            self.events.append(event_name)

    init_db()
    sid = "sess-history-abort"
    recorder = _LogRecorder()

    def _aborting_read(s: Any, session_id: str, **kwargs: Any) -> Any:
        # 22012: Postgres marks the whole transaction aborted, so every later
        # statement fails 25P02 until something rolls back to a savepoint.
        s.execute(text("SELECT 1 / 0"))
        raise AssertionError("unreachable: the division must raise")

    monkeypatch.setattr(conv, "_recent_rows", _aborting_read)
    monkeypatch.setattr(conv, "log", recorder)

    ctx = conv.open_turn(question="What about dissolution?", session_id=sid)

    assert ctx.degraded is False
    assert ctx.session_id == sid
    assert ctx.recent_turns == []
    assert "get_recent_turns_failed" in recorder.events
    assert "session_setup_failed" not in recorder.events
    # Both writes COMMITTED despite the aborted read: without the savepoint the
    # commit itself fails and neither row exists.
    with session_scope() as s:
        assert s.get(ChatSession, sid) is not None
        rows = list(s.exec(select(ChatMessage).where(ChatMessage.session_id == sid)))
        assert [m.content for m in rows] == ["What about dissolution?"]


def test_open_turn_leaves_the_session_unowned_when_user_id_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bind_session=False parity for the dossier and whitepaper callers.

    Their synthetic Q&A must not appear in anyone's chat history, so the
    bookkeeping session stays unowned (invisible to /sessions) while the audit
    row still carries the attribution. Fails if open_turn ever starts binding
    the owner itself instead of taking the shell's already-resolved user_id.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_GROUNDED_TURN))

    result = qa_mod.ask(
        "What study design is recommended?", user_id="user-dossier", bind_session=False
    )

    assert result.status == "answer"
    with session_scope() as s:
        row = s.get(ChatSession, result.session_id)
        assert row is not None and row.user_id is None
        audit = s.get(QueryLog, result.audit_id)
        assert audit is not None and audit.user_id == "user-dossier"


# ---------- prompt shaping ----------


def test_single_turn_prompt_has_no_recent_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no history the user prompt is byte-identical to the single-turn form —
    it starts with the question and carries no conversation scaffolding."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    stub = _CapturingLLM(_GROUNDED_TURN)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)
    result = qa_mod.ask("What study design is recommended?")
    prompt = stub.user_prompts[-1]
    assert "Recent conversation" not in prompt
    assert prompt.startswith("<untrusted_question>\n")
    # The turn actually completes: a prompt assertion alone would still pass if
    # the structured payload never made it past the gate.
    assert result.status == "answer"


def test_followup_prompt_threads_prior_turn_without_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up carries the prior turn's PROSE as context, but its citation
    markers are stripped so the model cannot parrot a stale [PSG, p.N]."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    sid = "sess-ctx"
    conv.ensure_session(sid)
    _seed_turn(
        sid,
        # Markers on BOTH sides — a citation-shaped token in the prior QUESTION
        # must be stripped too, not just the answer's.
        q="What did [PSG_020503, p.9] recommend for the BE study?",
        a="A fed study is also recommended [PSG_020503, p.9].",
        status="answer",
        order=1,
    )
    stub = _CapturingLLM(_GROUNDED_TURN)
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)
    result = qa_mod.ask("What study design is recommended?", session_id=sid)
    prompt = stub.user_prompts[-1]
    recent_part = prompt.split("<untrusted_question>", 1)[0]
    assert "Recent conversation" in recent_part
    assert "fed study is also recommended" in recent_part  # answer prose threaded
    assert "recommend for the BE study" in recent_part  # question prose threaded
    assert not has_citation(recent_part)  # markers stripped from BOTH sides
    assert result.status == "answer"


def test_format_recent_drops_unbracketed_sources_trailer() -> None:
    """Legacy stored answers may carry unbracketed source entries that the
    bracket-only citation strip misses; the whole trailer must still be dropped."""
    turns = [
        PriorTurn(
            question="What dissolution method for albuterol?",
            answer=(
                "The USP paddle method is recommended.\n\n"
                "Sources:\n- PSG_020503, p.4: dissolution method"
            ),
            status="answer",
        )
    ]
    block = qa_mod._format_recent(turns)
    assert "USP paddle method is recommended." in block  # prose still threads
    assert "PSG_020503" not in block
    assert "p.4" not in block
    assert "Sources" not in block


def test_format_recent_strips_bare_numeric_markers() -> None:
    """A stale model-facing [n] in stored history must never read as a live
    pointer into THIS turn's passage numbering (v6 prose). Pair markers and the
    Sources trailer are already handled; the numeric strip is unconditional and
    deletion-only, so unmatched history text stays byte-identical."""
    turns = [
        PriorTurn(
            question="What about the fed arm [2]?",
            answer=("A fed study is described [1, 2]. See section II.A.\n\nSources:\n- [3]"),
            status="answer",
        )
    ]
    block = qa_mod._format_recent(turns)
    assert "[2]" not in block
    assert "[1, 2]" not in block
    assert "fed arm" in block  # question prose still threads
    assert "fed study is described" in block  # answer prose still threads
    assert "Sources" not in block


# ---------- the load-bearing INV-1 property under multi-turn ----------


def test_followup_stale_prior_citation_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A follow-up answer that re-cites a passage from a PRIOR turn which is NOT in
    the current retrieval set is dropped to a refusal: a fact that lived only in a
    prior turn cannot acquire a valid citation now (INV-1 holds under memory)."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    sid = "sess-inv1"
    conv.ensure_session(sid)
    _seed_turn(
        sid,
        q="What BE study for albuterol?",
        a="A fed study is recommended [PSG_020503, p.9].",
        status="answer",
        order=1,
    )
    # Adversarial: the model parrots the prior fed-study fact and DECLARES the
    # PRIOR page (p.9) as its citation -- a page that is not among this turn's
    # passages (only p.3 was retrieved). The claim is the turn's only claim, so
    # dropping it leaves nothing admitted and the turn refuses.
    parroted = synth_turn_json([("A fed study is recommended", [("PSG_020503", 9)])])
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(parroted))
    result = qa_mod.ask("What study design is recommended?", session_id=sid)
    assert result.refused
    assert "p.9" not in result.answer
    assert result.answer == get_settings().refusal_text
    # No citation may survive the drop, and the decline must be the CORPUS-silent
    # one (no_valid_citations), not a machine-fault or model-decline branch --
    # those would record a different assertion in the audit row.
    assert result.citations == []
    assert result.reason == "no_valid_citations"
