"""INV-6: every /assemble — refused or successful — writes a query_log row.

`build_dossier` previously logged nothing on the early no-matching-PSG refusal
and relied solely on the inner `ask()` (mode="qa") on the success path, so no
row was ever written under mode="assemble". These tests lock that both paths
now audit under the correct mode.

Also locks the dossier→Q&A filter handoff for multi-form drugs: when the
matched PSGs agree on one (dosage_form, route) combo the dossier pins it on the
inner ask() (no clarify inside a dossier); when they disagree it pins only the
product and never invents a form (INV-5).
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


def _seed_multiform_estradiol() -> None:
    """Two estradiol PSG docs in DIFFERENT (dosage_form, route) combos."""
    with session_scope() as s:
        for appl, form, route in (
            ("020001", "Gel", "Transdermal"),
            ("020002", "Tablet", "Vaginal"),
        ):
            s.add(
                PsgDocument(
                    active_ingredient="Estradiol",
                    normalized_name="estradiol",
                    dosage_form=form,
                    route=route,
                    appl_no=appl,
                    psg_type="draft",
                    recommended_date="2020-01-01",
                    source_url=f"http://example/PSG_{appl}.pdf",
                    content_hash=f"hash-{appl}",
                )
            )


def _capture_ask(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the inner Q&A, recording the filters the dossier pins on it."""
    captured: list[dict[str, Any]] = []

    def _fake_ask(question: str, *, filters: dict[str, Any] | None = None, **_kw: Any) -> QAResult:
        captured.append(dict(filters or {}))
        return QAResult(
            answer="BE study guidance.",
            citations=[],
            refused=False,
            model_name="echo-model",
            audit_id=1,
            retrieved=[],
            status="answer",
        )

    monkeypatch.setattr(dossier_mod, "ask", _fake_ask)
    monkeypatch.setattr(dossier_mod, "_fetch_rld_label", lambda *a, **k: None)
    return captured


def test_dossier_pins_single_matched_combo_on_inner_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Multi-form drug, but the requested dosage form narrows the matched PSGs to
    # ONE (dosage_form, route) combo → the dossier pins it on the inner ask() so
    # the Q&A's multi-form guard doesn't start clarifying inside a dossier.
    init_db()
    _seed_multiform_estradiol()
    captured = _capture_ask(monkeypatch)

    result = build_dossier(active_ingredient="Estradiol", dosage_form="Gel", rld=None)

    assert result["refused"] is False
    assert captured == [
        {"normalized_name": "estradiol", "dosage_form": "Gel", "route": "Transdermal"}
    ]


def test_dossier_does_not_pin_form_when_combos_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No dosage form requested and the matched PSGs span TWO combos → the dossier
    # must not invent one (INV-5); it pins only the product and lets the inner
    # Q&A's multi-form guard surface the ambiguity honestly.
    init_db()
    _seed_multiform_estradiol()
    captured = _capture_ask(monkeypatch)

    result = build_dossier(active_ingredient="Estradiol", dosage_form=None, rld=None)

    assert result["refused"] is False
    assert captured == [{"normalized_name": "estradiol"}]


def test_dossier_renders_per_form_note_when_inner_qa_clarifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Multi-form drug, no form requested → the inner Q&A clarifies (status
    # "clarify"). A dossier is non-interactive: it must NOT embed the dangling
    # clarify prompt as Section D content. Instead it states the forms explicitly
    # and surfaces qa_status so callers can detect the ambiguity.
    init_db()
    _seed_multiform_estradiol()

    clarify_result = QAResult(
        answer="Estradiol has FDA guidance for more than one dosage form. "
        "Which form did you mean?",
        citations=[],
        refused=False,
        model_name="echo-model",
        audit_id=1,
        retrieved=[],
        status="clarify",
        reason="multi_form",
    )
    monkeypatch.setattr(dossier_mod, "ask", lambda *a, **k: clarify_result)
    monkeypatch.setattr(dossier_mod, "_fetch_rld_label", lambda *a, **k: None)

    result = build_dossier(active_ingredient="Estradiol", dosage_form=None, rld=None)

    assert result["refused"] is False
    # The interactive clarify question is NOT embedded as document content.
    assert "Which form did you mean?" not in result["markdown"]
    # The forms are stated explicitly so the document is self-describing.
    assert "Gel (Transdermal)" in result["markdown"]
    assert "Tablet (Vaginal)" in result["markdown"]
    # API callers can detect the multi-form ambiguity instead of treating the note
    # as a normal answer.
    assert result["sections"]["qa_status"] == "clarify"
