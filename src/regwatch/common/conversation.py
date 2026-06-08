"""Conversation persistence and deterministic session context.

Conversation memory can make follow-up questions ergonomic, but it is not FDA
evidence. This module stores turns and only carries forward narrow, deterministic
filters such as the selected product.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from regwatch.store.db import session_scope
from regwatch.store.models import ChatMessage, ChatSession

SESSION_FILTER_KEYS = frozenset({"normalized_name", "dosage_form", "route", "psg_type", "doc_id"})


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
    """Return an existing or newly-created session id."""
    sid = session_id or str(uuid4())
    now = datetime.now(UTC)
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        if row is None:
            row = ChatSession(id=sid, user_id=user_id, created_at=now, updated_at=now)
        else:
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


def update_session_filters(session_id: str | None, filters: dict[str, Any] | None) -> None:
    safe = _safe_filters(filters)
    if not session_id or not safe.get("normalized_name"):
        return
    now = datetime.now(UTC)
    with session_scope() as s:
        row = s.get(ChatSession, session_id)
        if row is None:
            row = ChatSession(id=session_id, created_at=now)
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
    filters: dict[str, Any] | None = None,
    citations: list[dict[str, Any]] | None = None,
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
                filters_json=_safe_filters(filters),
                citations_json=citations or [],
                metadata_json=metadata or {},
                created_at=datetime.now(UTC),
            )
        )
    return message_id
