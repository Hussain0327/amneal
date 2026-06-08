"""Durable audit log for every Q&A / assemble / watch interaction (INV-6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog


def log_query(
    *,
    mode: str,
    query_text: str,
    retrieved: list[dict[str, Any]],
    answer_text: str,
    citations: list[dict[str, Any]],
    refused: bool,
    model_name: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    status: str | None = None,
    route_json: dict[str, Any] | None = None,
) -> int:
    """Persist a query record and return its id."""
    with session_scope() as s:
        row = QueryLog(
            ts=datetime.now(UTC),
            session_id=session_id,
            turn_id=turn_id,
            mode=mode,
            query_text=query_text,
            retrieved_json=retrieved,
            answer_text=answer_text,
            citations_json=citations,
            refused=refused,
            status=status,
            route_json=route_json or {},
            model_name=model_name,
        )
        s.add(row)
        s.flush()
        if row.id is None:
            raise RuntimeError("query_log insert did not produce an id")
        return row.id
