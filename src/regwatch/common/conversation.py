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
from sqlmodel import col, select

from regwatch.store.db import session_scope
from regwatch.store.models import ChatMessage, ChatSession

SESSION_FILTER_KEYS = frozenset({"normalized_name", "dosage_form", "route", "psg_type", "doc_id"})


class SessionOwnershipError(RuntimeError):
    """A caller tried to bind a chat session owned by a different user."""


def new_turn_id() -> str:
    return str(uuid4())


def _safe_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in SESSION_FILTER_KEYS:
        value = (filters or {}).get(key)
        if value not in (None, "", []):
            out[key] = value
    return out


def ensure_session(session_id: str | None = None, *, user_id: str | None = None) -> str:
    """Return an existing or newly-created session id.

    Raises SessionOwnershipError when the row already belongs to a different
    user — the last line of defense when an API-level ownership check loses a
    race to a concurrent adopter, so a lost race aborts instead of writing
    this caller's turns into another user's session.
    """
    sid = session_id or str(uuid4())
    now = datetime.now(UTC)
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        if row is None:
            row = ChatSession(id=sid, user_id=user_id, created_at=now, updated_at=now)
        else:
            if user_id and row.user_id and row.user_id != user_id:
                raise SessionOwnershipError(sid)
            if user_id and row.user_id is None:
                row.user_id = user_id
            row.updated_at = now
        s.add(row)
    return sid


def get_session_filters(session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return {}
    with session_scope() as s:
        row = s.get(ChatSession, session_id)
        if row is None:
            return {}
        return _safe_filters(dict(row.active_filters_json or {}))


@dataclass
class PriorTurn:
    """One completed prior turn, for conversational context only (never evidence)."""

    question: str
    answer: str
    status: str | None


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
            rows = s.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(desc(col(ChatMessage.created_at)))
                # Scan a bounded window (enough to find `limit` answer turns past
                # interleaved refusals/clarifies) without loading a whole long
                # conversation.
                .limit(limit * 8)
            ).all()
            # Extract plain fields WHILE the session is open — the ORM rows detach
            # and their attributes expire once the scope commits/closes, so all
            # column access must happen here, not in the folding loop below.
            raw = [(m.turn_id, m.role, m.content or "", m.status) for m in rows]
    except Exception:
        # Conversational memory is an ergonomic aid, never required for
        # correctness — degrade to no memory rather than fail the turn.
        return []

    # `raw` is newest-first; fold into turns keyed by turn_id, preserving order.
    by_turn: dict[str, dict[str, tuple[str, str | None]]] = {}
    order: list[str] = []
    for turn_id, role, content, status in raw:
        if exclude_turn_id and turn_id == exclude_turn_id:
            continue
        if turn_id not in by_turn:
            by_turn[turn_id] = {}
            order.append(turn_id)
        by_turn[turn_id].setdefault(role, (content, status))  # keep newest per role

    turns: list[PriorTurn] = []
    for tid in order:  # newest-first
        slot = by_turn[tid]
        user = slot.get("user")
        assistant = slot.get("assistant")
        if user is None or assistant is None:
            continue
        answer, a_status = assistant
        # An answer/summary is the only kind of prior turn with a fact to thread;
        # None-status (older/legacy rows) is treated as an answer, not dropped.
        if (a_status or "answer") not in ("answer", "summary"):
            continue
        turns.append(PriorTurn(question=user[0].strip(), answer=answer.strip(), status=a_status))
        if len(turns) >= limit:
            break
    turns.reverse()  # oldest-first for the prompt
    return turns


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
