"""Conversation persistence and deterministic session context.

Conversation memory can make follow-up questions ergonomic, but it is not FDA
evidence. This module stores turns and only carries forward narrow, deterministic
filters such as the selected product.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import desc
from sqlmodel import Session, col, select

from regwatch.common.logging import get_logger
from regwatch.store.db import session_scope
from regwatch.store.models import ChatMessage, ChatSession

log = get_logger(__name__)

SESSION_FILTER_KEYS = frozenset(
    {"normalized_name", "dosage_form", "route", "psg_type", "doc_id", "appl_no"}
)

# Which surface a chat_session belongs to (issue #208). "thread" is the
# analyst's real work, listed in the work rail's Threads list. "assistant" is
# the Research Studio panel's own scratch conversation: kept (readable and
# deletable by id), but filtered out of that list so it never buries real work.
SESSION_ORIGIN_THREAD = "thread"
SESSION_ORIGIN_ASSISTANT = "assistant"
SESSION_ORIGINS = frozenset({SESSION_ORIGIN_THREAD, SESSION_ORIGIN_ASSISTANT})


class SessionOwnershipError(RuntimeError):
    """A caller tried to bind a chat session owned by a different user."""


class SessionOriginError(ValueError):
    """An origin outside SESSION_ORIGINS reached a session write.

    Its own type, for the same reason SessionOwnershipError has one:
    ``open_turn`` degrades to a fresh id on a generic Exception, and this must
    NOT degrade -- it is a programming error in an internal caller, not the DB
    hiccup that degrade path exists for. A bare ValueError would force that
    degrade to re-raise every ValueError raised inside the scope it wraps,
    including unrelated ones from the user-message write. Subclasses ValueError
    so existing `except ValueError` callers keep working.
    """


def new_turn_id() -> str:
    return str(uuid4())


def _safe_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in SESSION_FILTER_KEYS:
        value = (filters or {}).get(key)
        if value not in (None, "", []):
            out[key] = value
    return out


def _check_origin(origin: str) -> None:
    """Rejects an origin outside SESSION_ORIGINS, before any I/O.

    Args:
        origin: The caller-supplied session origin.

    Raises:
        SessionOriginError: The origin is not one of SESSION_ORIGINS.
    """
    if origin not in SESSION_ORIGINS:
        raise SessionOriginError(f"unknown session origin: {origin!r}")


def _ensure_session_row(
    s: Session,
    session_id: str,
    *,
    user_id: str | None,
    origin: str,
    now: datetime,
) -> ChatSession:
    """Creates, adopts or touches one chat_session row inside a caller's scope.

    The single copy of that query: ``ensure_session`` and ``open_turn`` both go
    through here, so ownership and the create-only origin rule are decided in
    exactly one place no matter which entry point a caller used.

    Args:
        s: An open Session; the CALLER owns the transaction and the commit.
        session_id: The session id to create or continue.
        user_id: The owner to bind, or None to leave the row unowned.
        origin: Applied ONLY when the row is created.
        now: The turn clock, written to updated_at (and created_at on create).

    Returns:
        The live ChatSession row, freshly added or already loaded into ``s``.

    Raises:
        SessionOwnershipError: The row already belongs to a different user.
    """
    row = s.get(ChatSession, session_id)
    if row is None:
        row = ChatSession(
            id=session_id, user_id=user_id, origin=origin, created_at=now, updated_at=now
        )
    else:
        if user_id and row.user_id and row.user_id != user_id:
            raise SessionOwnershipError(session_id)
        if user_id and row.user_id is None:
            row.user_id = user_id
        row.updated_at = now
    s.add(row)
    return row


def ensure_session(
    session_id: str | None = None,
    *,
    user_id: str | None = None,
    origin: str = SESSION_ORIGIN_THREAD,
) -> str:
    """Return an existing or newly-created session id.

    Raises SessionOwnershipError when the row already belongs to a different
    user — the last line of defense when an API-level ownership check loses a
    race to a concurrent adopter, so a lost race aborts instead of writing
    this caller's turns into another user's session.

    ``origin`` is applied ONLY when the row is created; an existing row's
    origin is never touched here, the same rule active_filters_json already
    follows -- a session already established as one kind does not change kind
    because a later call against the same id passes a different origin.
    Raises SessionOriginError (a ValueError) for an origin outside
    SESSION_ORIGINS, BEFORE the write: the API boundary (QueryRequest) already
    validates this, so reaching here with junk is a programming error in an
    internal caller and must fail loudly rather than hit the DB CHECK.
    """
    _check_origin(origin)
    sid = session_id or str(uuid4())
    now = datetime.now(UTC)
    with session_scope() as s:
        _ensure_session_row(s, sid, user_id=user_id, origin=origin, now=now)
    return sid


def _filters_from_row(row: ChatSession | None) -> dict[str, Any]:
    """Extracts a session's carry-over filters from an already-loaded row.

    Split out so a caller that ALREADY holds the ChatSession row (``open_turn``
    holds the one it just upserted) reads the filters off that object instead of
    issuing a second SELECT for the same row.

    Args:
        row: The loaded session row, or None when the session does not exist.

    Returns:
        The safe carry-over filters, possibly empty.
    """
    if row is None:
        return {}
    return _safe_filters(dict(row.active_filters_json or {}))


def get_session_filters(session_id: str | None) -> dict[str, Any]:
    """Deterministic carry-over filters for a session (or ``{}`` when unknown).

    Best-effort, mirroring ``get_recent_turns``: session context is an
    ergonomic aid, never required for correctness -- any DB error degrades to
    ``{}`` (logged) rather than failing an otherwise-answerable turn.
    """
    if not session_id:
        return {}
    try:
        with session_scope() as s:
            return _filters_from_row(s.get(ChatSession, session_id))
    except Exception:
        log.warning("get_session_filters_failed", exc_info=True)
        return {}


@dataclass
class PriorTurn:
    """One completed prior turn, for conversational context only (never evidence)."""

    question: str
    answer: str
    status: str | None
    # Application-owned labels for the advisory route prompt. Product scope is
    # derived from the persisted assistant filters and a real audit id. Corpus
    # is deliberately never inferred from route-call shadow output; PR12 may
    # populate it only after a corpus scope actually executes and is audited.
    scope_kind: str = "none"
    scope_audited: bool = False
    corpus_policy: str | None = None
    audit_id: int | None = None


@dataclass(frozen=True)
class _StoredMessage:
    content: str
    status: str | None
    filters: dict[str, Any]
    audit_id: int | None


def _recent_rows(
    s: Session,
    session_id: str,
    *,
    limit: int,
) -> list[tuple[str, str, _StoredMessage]]:
    """Fetches the raw message window for a session inside a caller's scope.

    The I/O half of ``get_recent_turns``: it takes an OPEN Session so a caller
    that already has one (``open_turn``, ``read_turn_context``) reads history in
    the same transaction instead of opening a second one.

    Args:
        s: An open Session; the caller owns the transaction.
        session_id: The session whose messages to read.
        limit: How many completed turns the fold is aiming for; the scanned
            window is a bounded multiple of it.

    Returns:
        (turn_id, role, message) triples, NEWEST first, with every column value
        already copied out of the ORM rows.
    """
    if limit <= 0:
        return []
    rows = s.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(desc(col(ChatMessage.created_at)))
        # Scan a bounded window (enough to find `limit` answer turns past
        # interleaved refusals/clarifies) without loading a whole long
        # conversation.
        .limit(limit * 8)
    ).all()
    # Extract plain fields WHILE the session is open -- the ORM rows detach
    # and their attributes expire once the scope commits/closes, so all
    # column access must happen here, not in the folding loop.
    return [
        (
            m.turn_id,
            m.role,
            _StoredMessage(
                content=m.content or "",
                status=m.status,
                filters=_safe_filters(dict(m.filters_json or {})),
                audit_id=m.audit_id,
            ),
        )
        for m in rows
    ]


def _fold_recent(
    raw: list[tuple[str, str, _StoredMessage]],
    *,
    limit: int,
    exclude_turn_id: str | None,
) -> list[PriorTurn]:
    """Folds a newest-first message window into completed prior turns.

    Pure: no I/O, so it runs AFTER the reading scope has closed.

    Args:
        raw: (turn_id, role, message) triples, newest first.
        limit: Maximum turns to return.
        exclude_turn_id: The in-flight turn, whose own messages are skipped.

    Returns:
        Up to ``limit`` answer/summary turns, OLDEST first.
    """
    # `raw` is newest-first; fold into turns keyed by turn_id, preserving order.
    by_turn: dict[str, dict[str, _StoredMessage]] = {}
    order: list[str] = []
    for turn_id, role, stored in raw:
        if exclude_turn_id and turn_id == exclude_turn_id:
            continue
        if turn_id not in by_turn:
            by_turn[turn_id] = {}
            order.append(turn_id)
        by_turn[turn_id].setdefault(role, stored)  # keep newest per role

    turns: list[PriorTurn] = []
    for tid in order:  # newest-first
        slot = by_turn[tid]
        user = slot.get("user")
        assistant = slot.get("assistant")
        if user is None or assistant is None:
            continue
        answer, a_status = assistant.content, assistant.status
        # An answer/summary is the only kind of prior turn with a fact to thread;
        # None-status (older/legacy rows) is treated as an answer, not dropped.
        if (a_status or "answer") not in ("answer", "summary"):
            continue
        product_audited = bool(
            assistant.audit_id is not None
            and assistant.audit_id > 0
            and assistant.filters.get("normalized_name")
        )
        turns.append(
            PriorTurn(
                question=user.content.strip(),
                answer=answer.strip(),
                status=a_status,
                scope_kind="product" if product_audited else "none",
                scope_audited=product_audited,
                audit_id=assistant.audit_id if product_audited else None,
            )
        )
        if len(turns) >= limit:
            break
    turns.reverse()  # oldest-first for the prompt
    return turns


def get_recent_turns(
    session_id: str | None,
    *,
    limit: int = 3,
    exclude_turn_id: str | None = None,
) -> list[PriorTurn]:
    """Up to ``limit`` most recent COMPLETED answer/summary turns, oldest-first.

    A turn is the (user, assistant) ``ChatMessage`` pair sharing a ``turn_id``.
    Only turns whose assistant reply is a real answer/summary are returned: a
    refused/clarify/meta turn carries no fact worth threading, and threading a
    refusal would re-inject the refusal sentence as "context". This is for
    conversational reference ONLY (pronoun/ellipsis resolution) — never FDA
    evidence; the caller strips citations and the synthesizer prompt forbids
    treating it as a source.

    Best-effort: no session, a non-positive limit, or any DB error returns ``[]``
    so a memory hiccup can never break or wrongly refuse an answerable query.
    """
    if not session_id or limit <= 0:
        return []
    try:
        with session_scope() as s:
            raw = _recent_rows(s, session_id, limit=limit)
    except Exception:
        # Conversational memory is an ergonomic aid, never required for
        # correctness -- degrade to no memory rather than fail the turn. But a
        # SILENT degrade hides a broken DB behind subtly context-less answers
        # (the recurring silent-failure incident class), so log it.
        log.warning("get_recent_turns_failed", exc_info=True)
        return []
    return _fold_recent(raw, limit=limit, exclude_turn_id=exclude_turn_id)


@dataclass(frozen=True)
class TurnContext:
    """Everything one turn needs from conversation storage, read in one scope.

    Frozen because the shell hands ``filters`` and ``recent_turns`` to the
    stateless core as pre-loaded CONSTANTS: nothing downstream may mutate the
    snapshot the turn was planned against.

    Attributes:
        session_id: The session the turn belongs to. On a degraded turn this is
            a FRESH id equal to ``turn_id``, never the requested one.
        turn_id: The turn's id, minted here when the caller passed none.
        filters: The session's deterministic carry-over filters, possibly empty.
        recent_turns: Conversation memory, oldest-first, possibly empty.
        degraded: True when session bookkeeping failed and the turn is running
            against a fresh id with no context (INV-6).
    """

    session_id: str
    turn_id: str
    filters: dict[str, Any]
    recent_turns: list[PriorTurn]
    degraded: bool = False


def open_turn(
    *,
    question: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    user_id: str | None = None,
    origin: str = SESSION_ORIGIN_THREAD,
    filters: dict[str, Any] | None = None,
    recent_limit: int = 3,
) -> TurnContext:
    """Opens one turn: session upsert, user-message write, and both reads, once.

    Replaces the four separate ``session_scope()``s a turn used to open
    (``ensure_session``, ``record_message``, ``get_session_filters``,
    ``get_recent_turns``) with ONE transaction. The carry-over filters cost no
    statement at all: they are read off the ChatSession row this call already
    loaded to upsert. The history read is eager, and still wins -- one statement
    inside an open transaction is cheaper than the pool checkout plus COMMIT a
    separate scope would have paid, even on a turn that never carries context.

    All four operations share one transaction, so a failure in either WRITE
    rolls the whole thing back rather than leaving an orphan ChatSession row
    behind. The history read is the exception: it keeps the best-effort
    contract ``get_recent_turns`` documents, isolated in a SAVEPOINT so its
    failure costs the turn its memory and nothing else.

    The reads are a SINGLE SNAPSHOT taken before the turn computes. A concurrent
    turn in the same session that commits mid-pipeline is no longer half-seen;
    that interleaving was already undefined and is now deterministic.

    Args:
        question: The user's literal question, persisted as the user message.
        session_id: An existing session to continue, or None to mint one.
        turn_id: A caller-minted turn id, or None to mint one.
        user_id: The owner to BIND to the session, or None to leave it unowned
            (the caller decides; ``bind_session`` never reaches here).
        origin: Applied only when the session row is created.
        filters: Caller-pinned filters, stored on the user message.
        recent_limit: Maximum prior turns to carry into this turn.

    Returns:
        The turn's ids and its pre-loaded context. On a DB failure, a DEGRADED
        context: a fresh session id equal to the turn id, no filters, no memory.

    Raises:
        SessionOwnershipError: The session already belongs to a different user.
            Nothing is written; the API maps this to its ownership 404.
        SessionOriginError: The origin was outside SESSION_ORIGINS. Raised
            before any I/O -- a caller bug, not the DB hiccup the degrade
            exists for.
    """
    _check_origin(origin)
    tid = turn_id or new_turn_id()
    sid = session_id or str(uuid4())
    now = datetime.now(UTC)
    try:
        with session_scope() as s:
            row = _ensure_session_row(s, sid, user_id=user_id, origin=origin, now=now)
            # Flush the SESSION row before the message is even queued.
            # chat_message.session_id is a foreign key to chat_session.id, but
            # neither model declares a relationship(), so the unit of work has
            # no inter-mapper dependency to sort the two INSERTs by and can
            # emit the message first -- a foreign-key violation on every new
            # session. Ordering here is not an optimization; it is correctness.
            s.flush()
            s.add(
                ChatMessage(
                    id=str(uuid4()),
                    session_id=sid,
                    turn_id=tid,
                    role="user",
                    content=question,
                    filters_json=_safe_filters(filters),
                    created_at=now,
                )
            )
            # Explicit, not autoflush-dependent: the message must reach the DB
            # before the history read below, whose window includes it (and
            # whose fold excludes it again by turn id).
            s.flush()
            carried = _filters_from_row(row)
            # SAVEPOINT, because the history read is the one BEST-EFFORT
            # statement in an otherwise load-bearing transaction. A cancelled
            # SELECT (statement_timeout, an admin pg_cancel_backend, a
            # serialization abort) leaves the transaction aborted, so without
            # this the read would take the session upsert and the user message
            # down with it and orphan the turn out of its thread. Rolling back
            # to the savepoint costs the turn only its memory -- exactly what
            # the standalone `get_recent_turns` reader cost before the two were
            # folded into one transaction.
            try:
                with s.begin_nested():
                    raw = _recent_rows(s, sid, limit=recent_limit)
            except Exception:
                raw = []
                log.warning("get_recent_turns_failed", exc_info=True)
    except (SessionOwnershipError, SessionOriginError):
        raise
    except Exception:
        # Session bookkeeping is best-effort: a DB hiccup must never stop the
        # query from being processed and audited (INV-6). Same event name the
        # shell logged before this function existed, so nothing operational
        # changes. Degrade to a FRESH id, never the requested one: after a
        # failed bind the requested session may belong to someone else, so
        # later writes must not target it.
        log.warning("session_setup_failed", exc_info=True)
        return TurnContext(session_id=tid, turn_id=tid, filters={}, recent_turns=[], degraded=True)
    return TurnContext(
        session_id=sid,
        turn_id=tid,
        filters=carried,
        recent_turns=_fold_recent(raw, limit=recent_limit, exclude_turn_id=tid),
    )


def read_turn_context(
    *,
    session_id: str,
    turn_id: str,
    recent_limit: int = 3,
) -> TurnContext:
    """Reads both halves of a turn's session context in one transaction.

    For a control plane that already performed the session write itself (the Go
    native path): there is no write here to piggyback on, but the two reads
    ``ask_core`` needs still share one scope, one pool checkout and one COMMIT
    instead of two each.

    Best-effort, exactly like the two readers it replaces: any DB error degrades
    to empty context (logged) rather than failing an answerable turn.

    Args:
        session_id: The session whose context to read.
        turn_id: The turn in flight; its own messages are excluded from memory.
        recent_limit: Maximum prior turns to carry into this turn.

    Returns:
        The turn's pre-loaded context, with ``degraded`` True when the read
        failed.
    """
    if not session_id:
        return TurnContext(session_id=session_id, turn_id=turn_id, filters={}, recent_turns=[])
    try:
        with session_scope() as s:
            carried = _filters_from_row(s.get(ChatSession, session_id))
            raw = _recent_rows(s, session_id, limit=recent_limit)
    except Exception:
        log.warning("session_context_read_failed", exc_info=True)
        return TurnContext(
            session_id=session_id,
            turn_id=turn_id,
            filters={},
            recent_turns=[],
            degraded=True,
        )
    return TurnContext(
        session_id=session_id,
        turn_id=turn_id,
        filters=carried,
        recent_turns=_fold_recent(raw, limit=recent_limit, exclude_turn_id=turn_id),
    )


def update_session_filters(session_id: str | None, filters: dict[str, Any] | None) -> None:
    safe = _safe_filters(filters)
    if not session_id or not safe.get("normalized_name"):
        return
    now = datetime.now(UTC)
    with session_scope() as s:
        row = s.get(ChatSession, session_id)
        if row is None:
            # Session deleted mid-flight (DELETE /sessions during a slow turn).
            # Never resurrect it: a recreated row would be owner-less and thus
            # adoptable by any authenticated user who knows the id.
            return
        row.active_filters_json = safe
        row.updated_at = now
        s.add(row)


def record_message(
    *,
    session_id: str,
    turn_id: str,
    role: str,
    content: str,
    status: str | None = None,
    model_name: str | None = None,
    audit_id: int | None = None,
    reason: str | None = None,
    interpretation: str | None = None,
    filters: dict[str, Any] | None = None,
    citations: list[dict[str, Any]] | None = None,
    clarify: list[dict[str, Any]] | None = None,
    related: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Persist one chat message and return its message id."""
    message_id = str(uuid4())
    with session_scope() as s:
        s.add(
            ChatMessage(
                id=message_id,
                session_id=session_id,
                turn_id=turn_id,
                role=role,
                content=content,
                status=status,
                model_name=model_name,
                audit_id=audit_id,
                reason=reason,
                interpretation=interpretation,
                filters_json=_safe_filters(filters),
                citations_json=citations or [],
                clarify_json=clarify or [],
                related_json=related or [],
                metadata_json=metadata or {},
                created_at=datetime.now(UTC),
            )
        )
    return message_id
