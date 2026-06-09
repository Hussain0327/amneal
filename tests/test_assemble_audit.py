"""INV-6: every /assemble — refused or successful — writes a query_log row.

`build_dossier` previously logged nothing on the early no-matching-PSG refusal
and relied solely on the inner `ask()` (mode="qa") on the success path, so no
row was ever written under mode="assemble". These tests lock that both paths
now audit under the correct mode.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import select

from regwatch.assemble import dossier as dossier_mod
from regwatch.assemble.dossier import build_dossier
from regwatch.generate.grounded_qa import QAResult
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, QueryLog

pytestmark = pytest.mark.invariants


def _assemble_logs() -> list[dict[str, Any]]:
    """Snapshot assemble query_log rows as plain dicts (rows detach on close)."""
    with session_scope() as s:
        rows = list(s.scalars(select(QueryLog).where(QueryLog.mode == "assemble")))
        return [
            {
                "mode": r.mode,
                "refused": r.refused,
                "query_text": r.query_text,
                "answer_text": r.answer_text,
                "model_name": r.model_name,
            }
            for r in rows
        ]


def test_refused_assemble_writes_assemble_query_log() -> None:
    init_db()
    result = build_dossier(
        active_ingredient="Imaginary Drug XYZ",
        dosage_form=None,
        rld=None,
    )
    assert result["refused"] is True

    rows = _assemble_logs()
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "assemble"
    assert row["refused"] is True
    assert "Imaginary Drug XYZ" in row["query_text"]
    assert row["model_name"]
    # The refusal marker is captured as the answer text (INV-6 visibility).
    assert "No PSG" in row["answer_text"]


def test_successful_assemble_writes_assemble_query_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    # A matching PSG must be present so the early-refusal path is not taken.
    with session_scope() as s:
        s.add(
            PsgDocument(
                active_ingredient="Albuterol Sulfate",
                normalized_name="albuterol sulfate",
                dosage_form="Aerosol, Metered",
                route="Inhalation",
                appl_no="020503",
                psg_type="draft",
                recommended_date="2020-01-01",
                source_url="http://example/PSG_020503.pdf",
                content_hash="hash-020503",
            )
        )

    # Stub the inner Q&A and the openFDA label fetch so the success path is
    # network-free and deterministic.
    qa_result = QAResult(
        answer="BE study guidance for albuterol sulfate.",
        citations=[],
        refused=False,
        model_name="echo-model",
        audit_id=1,
        retrieved=[{"chunk_id": "c1", "score": 0.9}],
        status="answer",
    )
    monkeypatch.setattr(dossier_mod, "ask", lambda *a, **k: qa_result)
    monkeypatch.setattr(dossier_mod, "_fetch_rld_label", lambda *a, **k: None)

    result = build_dossier(
        active_ingredient="Albuterol Sulfate",
        dosage_form="Aerosol, Metered",
        rld=None,
    )
    assert result["refused"] is False

    rows = _assemble_logs()
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "assemble"
    assert row["refused"] is False
    assert "Albuterol Sulfate" in row["query_text"]
    assert row["model_name"]
    # The assembled markdown is captured as the audit answer text.
    assert row["answer_text"] == result["markdown"]
