"""One transaction opens an Ask turn, not four.

A turn used to open four separate ``session_scope()``s before it computed
anything -- ensure_session, record_message, get_session_filters,
get_recent_turns -- each paying its own pool checkout, its own statements and
its own COMMIT. ``conversation.open_turn`` does all four inside ONE
transaction, and the carry-over filters cost no statement at all because they
are read off the ChatSession row the upsert already loaded.

These are round-trip tests: they count real SQL through a
``before_cursor_execute`` listener on the shared engine (the harness pattern
from tests/test_retrieval_no_graph.py), so they fail if any step reopens its
own scope or a second read of the same row reappears.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event
from sqlmodel import select

from regwatch.common import conversation as conv
from regwatch.generate import grounded_qa as qa_mod
from regwatch.store.db import get_engine, init_db, session_scope
from regwatch.store.models import ChatMessage, ChatSession
from tests.conftest import synth_turn_json
from tests.test_invariants import _meta, _seed_corpus, _stub_llm

pytestmark = pytest.mark.invariants

_QUESTION = "What study design is recommended?"
# One claim citing the single seeded passage, so the turn is fully admitted and
# the pipeline runs end to end instead of stopping at a parse failure.
_GROUNDED_TURN = synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])])


@contextmanager
def _sql_capture() -> Iterator[list[str]]:
    """Records every statement the shared engine executes inside the block.

    Yields:
        The live list of SQL statements, whitespace-normalized.
    """
    engine = get_engine()
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

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _capture)


@contextmanager
def _sql_and_commit_capture() -> Iterator[list[tuple[str, str]]]:
    """Records statements AND commits, in order, for the shared engine.

    Interleaving the two is what proves a pair of reads shared one transaction:
    a COMMIT between them means two scopes, however low the statement count is.

    Yields:
        The live ordered list of ("sql", statement) / ("commit", "") events.
    """
    engine = get_engine()
    events: list[tuple[str, str]] = []

    def _capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        events.append(("sql", " ".join(statement.split())))

    def _on_commit(conn: Any) -> None:
        events.append(("commit", ""))

    event.listen(engine, "before_cursor_execute", _capture)
    event.listen(engine, "commit", _on_commit)
    try:
        yield events
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        event.remove(engine, "commit", _on_commit)


def _label(statement: str) -> str | None:
    """Names the chat-table operation a statement performs.

    Args:
        statement: A whitespace-normalized SQL statement.

    Returns:
        A short label, or None when the statement touches neither chat table --
        which makes an unexpected statement fail the exact-sequence assertions
        rather than slip through them.
    """
    if "FROM chat_session" in statement:
        return "select_session"
    if "INSERT INTO chat_session" in statement:
        return "insert_session"
    if "UPDATE chat_session" in statement:
        return "update_session"
    if "INSERT INTO chat_message" in statement:
        return "insert_message"
    if "FROM chat_message" in statement:
        return "select_messages"
    # Named, not ignored: the savepoint pair is the price of keeping the
    # history read best-effort inside the write transaction, so it belongs in
    # the exact sequences below where a third one would be noticed.
    if statement.startswith("RELEASE SAVEPOINT"):
        return "release_savepoint"
    if statement.startswith("SAVEPOINT"):
        return "savepoint"
    return None


def _before_audit(statements: list[str]) -> list[str]:
    """Returns the statements a turn executed BEFORE its audit row was written.

    That prefix is the turn's read path. The post-turn filter carry-over write
    reads the session row again on purpose (``_apply_session_patch`` keeps its
    independent best-effort writes), and that is not what these tests measure.

    Args:
        statements: Every statement captured during one ``ask()`` call.

    Returns:
        The prefix executed before the audit INSERT.

    Raises:
        AssertionError: The turn wrote no audit row, so the capture is not
            measuring a real turn (INV-6 would be broken anyway).
    """
    for index, statement in enumerate(statements):
        if "INSERT INTO query_log" in statement:
            return statements[:index]
    raise AssertionError("the captured turn wrote no audit row")


def test_open_turn_uses_one_checkout_one_commit_and_four_statements() -> None:
    """The whole point of the item, mechanically.

    One pool checkout, one COMMIT, and exactly four chat-table statements: the
    session read, the session write, the user-message INSERT and the history
    read. A SECOND `FROM chat_session` here would mean the carry-over filters
    went back to the database instead of coming off the row already in memory.

    The SAVEPOINT/RELEASE pair around the history read is the one accepted
    addition -- two cheap in-transaction statements that buy the read its
    best-effort contract back (see conversation.open_turn). Naming them here
    keeps the sequence exact, so a THIRD savepoint or a stray statement still
    fails.
    """
    init_db()
    engine = get_engine()
    checkouts: list[int] = []
    commits: list[int] = []

    def _on_checkout(dbapi_connection: Any, record: Any, proxy: Any) -> None:
        checkouts.append(1)

    def _on_commit(conn: Any) -> None:
        commits.append(1)

    event.listen(engine, "checkout", _on_checkout)
    event.listen(engine, "commit", _on_commit)
    try:
        with _sql_capture() as cold:
            ctx = conv.open_turn(question="A cold turn", session_id="sess-round-trips")
    finally:
        event.remove(engine, "checkout", _on_checkout)
        event.remove(engine, "commit", _on_commit)

    assert ctx.session_id == "sess-round-trips"
    assert ctx.degraded is False
    assert len(checkouts) == 1, f"expected one pool checkout, got {len(checkouts)}"
    assert len(commits) == 1, f"expected one commit, got {len(commits)}"
    assert [_label(s) for s in cold] == [
        "select_session",
        "insert_session",
        "insert_message",
        "savepoint",
        "select_messages",
        "release_savepoint",
    ]

    # The warm path differs only in swapping the session INSERT for the touch.
    with _sql_capture() as warm:
        conv.open_turn(question="A warm turn", session_id="sess-round-trips")
    assert [_label(s) for s in warm] == [
        "select_session",
        "update_session",
        "insert_message",
        "savepoint",
        "select_messages",
        "release_savepoint",
    ]


def test_ask_reads_chat_session_once_per_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reduction survives end to end, not just in the unit.

    Before this item a turn read chat_session TWICE before computing
    (ensure_session, then get_session_filters at the carry-over site), so this
    fails on the previous shape -- and fails again if anyone reintroduces a
    lazy per-site read on the relay path.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_GROUNDED_TURN))

    with _sql_capture() as statements:
        result = qa_mod.ask(_QUESTION)

    assert result.status == "answer"
    labels = [_label(s) for s in _before_audit(statements)]
    assert labels.count("select_session") == 1
    assert labels.count("select_messages") == 1
    assert labels.count("insert_message") == 1


def test_open_turn_is_atomic_when_a_write_path_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-scope failure rolls the writes back instead of orphaning them.

    The writes share one transaction, so the state where the session row is
    committed but the turn degraded cannot exist. Before this item
    ensure_session committed independently and left that orphan behind.

    The failure is injected AFTER both writes flushed, which is the only place
    that distinguishes a rollback from a no-op. The history read is deliberately
    NOT the injection point any more: it is best-effort and savepointed, so its
    failure must leave these writes committed -- pinned by
    test_conversational_memory.test_open_turn_history_failure_does_not_roll_back_the_writes.
    """
    init_db()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated carry-over failure")

    monkeypatch.setattr(conv, "_filters_from_row", _boom)

    ctx = conv.open_turn(
        question="Does this leave anything behind?",
        session_id="sess-atomic",
        turn_id="turn-atomic",
    )

    assert ctx.degraded is True
    # A FRESH id, never the requested one: after a failed bind the requested
    # session may belong to someone else.
    assert ctx.session_id == ctx.turn_id == "turn-atomic"
    assert ctx.filters == {}
    assert ctx.recent_turns == []
    with session_scope() as s:
        assert s.get(ChatSession, "sess-atomic") is None
        rows = list(s.exec(select(ChatMessage).where(ChatMessage.turn_id == "turn-atomic")))
        assert rows == []


def test_compute_turn_reads_session_context_once_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Go native path, where production traffic actually flows.

    compute_turn has no write to piggyback on, so instead of ``open_turn`` it
    gets one lazily-fired scope issuing BOTH context reads. Before this item
    each loader opened its own scope, and the route-shadow block called them
    again outside the core's memo. Pins three things: one read of each table,
    no COMMIT between them (so it really is one transaction), and the
    top-level ``session_context`` stage ITEM 5 keys off.
    """
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(_GROUNDED_TURN))
    sid = "sess-native"
    conv.ensure_session(sid)
    conv.update_session_filters(sid, {"normalized_name": "albuterol sulfate"})
    conv.record_message(session_id=sid, turn_id="t-prior", role="user", content="Q1")
    conv.record_message(
        session_id=sid, turn_id="t-prior", role="assistant", content="A1", status="answer"
    )

    with _sql_and_commit_capture() as events:
        _outcome, audit, _patch = qa_mod.compute_turn(_QUESTION, session_id=sid, turn_id="t-now")

    labels = [_label(text) if kind == "sql" else kind for kind, text in events]
    assert labels.count("select_session") == 1
    assert labels.count("select_messages") == 1
    # compute_turn writes nothing at all: Go owns every write on this path.
    assert "insert_message" not in labels
    assert "insert_session" not in labels
    assert "update_session" not in labels
    session_at = labels.index("select_session")
    messages_at = labels.index("select_messages")
    assert "commit" not in labels[session_at:messages_at], "the two reads were not one transaction"

    timings = audit.route_json["timings"]
    assert timings["session_context_ms"] >= 0

    # ...and the combined read means exactly what the two helpers it replaced
    # meant, so the native path's context is unchanged.
    ctx = conv.read_turn_context(session_id=sid, turn_id="t-now")
    assert ctx.degraded is False
    assert ctx.filters == conv.get_session_filters(sid)
    assert ctx.recent_turns == conv.get_recent_turns(sid, limit=3, exclude_turn_id="t-now")
    assert [t.question for t in ctx.recent_turns] == ["Q1"]
