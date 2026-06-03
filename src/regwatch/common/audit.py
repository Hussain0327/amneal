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
) -> int:
    """Persist a query record and return its id."""
    with session_scope() as s:
        row = QueryLog(
            ts=datetime.now(UTC),
            mode=mode,
            query_text=query_text,
            retrieved_json=retrieved,
            answer_text=answer_text,
            citations_json=citations,
            refused=refused,
            model_name=model_name,
        )
        s.add(row)
        s.flush()
        assert row.id is not None
        return row.id
