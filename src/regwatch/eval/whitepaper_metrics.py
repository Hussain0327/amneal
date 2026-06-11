"""Eval metrics for the White-Paper populator.

The grounded-Q&A gold set scores ``ask()`` answers; the white paper produces a
different shape (a set of cells, each with a mode/status/value/evidence), so it
needs its own scorer that maps onto the SAME thresholds (recall@k >= 0.90,
citation_precision >= 0.95, refusal_accuracy >= 0.95).

A ``WhitepaperGoldItem`` asserts the expected outcome of one cell:

  - ``expect_status``      — the tri-state status the cell must land on. A manual
    cell asserting ``analyst_input_required`` is graded like ``must_refuse``: the
    correctness is the DECISION, folded into ``refusal_accuracy``.
  - ``expect_value_contains`` — a substring the populated value must contain
    (answer CONTENT, like fact_recall). Folds into ``recall_at_k``.
  - ``expect_evidence_source`` — a substring at least one evidence row's source
    must contain (provenance, like citation_precision).

Mapping to the shared threshold names so the gate is uniform:
  - recall_at_k          = fraction of value-bearing items whose value matched.
  - citation_precision   = fraction of populated items carrying expected evidence.
  - refusal_accuracy     = fraction of items whose STATUS matched (the decision).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WhitepaperGoldItem:
    cell_id: str
    expect_status: str  # populated | analyst_input_required | verified_absent
    expect_value_contains: str | None = None
    expect_evidence_source: str | None = None


def default_gold_path() -> Path:
    return Path(__file__).parent / "whitepaper_gold.jsonl"


def load_whitepaper_gold(path: Path | None = None) -> list[WhitepaperGoldItem]:
    """Load gold items from a JSONL file (``#`` comment lines skipped)."""
    target = path or default_gold_path()
    items: list[WhitepaperGoldItem] = []
    with target.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            items.append(
                WhitepaperGoldItem(
                    cell_id=row["cell_id"],
                    expect_status=row["expect_status"],
                    expect_value_contains=row.get("expect_value_contains"),
                    expect_evidence_source=row.get("expect_evidence_source"),
                )
            )
    return items


@dataclass
class WhitepaperScorecard:
    n: int = 0
    recall_at_k: float = 0.0
    citation_precision: float = 0.0
    refusal_accuracy: float = 0.0
    status_correct: int = 0
    value_checked: int = 0
    value_correct: int = 0
    evidence_checked: int = 0
    evidence_correct: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def _index_cells(result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for section in result["sections"]:
        for cell in section["cells"]:
            out[cell["id"]] = cell
    return out


def evaluate_whitepaper(
    result: Mapping[str, Any],
    items: list[WhitepaperGoldItem],
) -> WhitepaperScorecard:
    """Grade a built white paper against per-cell gold expectations."""
    if not items:
        return WhitepaperScorecard()
    cells = _index_cells(result)
    status_correct = 0
    value_checked = value_correct = 0
    evidence_checked = evidence_correct = 0
    details: list[dict[str, Any]] = []

    for item in items:
        cell = cells.get(item.cell_id)
        if cell is None:
            details.append({"cell_id": item.cell_id, "error": "cell not found"})
            continue
        status_ok = cell["status"] == item.expect_status
        status_correct += int(status_ok)

        value_ok: bool | None = None
        if item.expect_value_contains is not None:
            value_checked += 1
            value = (cell.get("value") or "").lower()
            value_ok = item.expect_value_contains.lower() in value
            value_correct += int(value_ok)

        evidence_ok: bool | None = None
        if item.expect_evidence_source is not None:
            evidence_checked += 1
            want = item.expect_evidence_source.lower()
            evidence_ok = any(
                want in str(ev.get("source") or "").lower() for ev in cell["evidence"]
            )
            evidence_correct += int(evidence_ok)

        details.append(
            {
                "cell_id": item.cell_id,
                "status": cell["status"],
                "status_ok": status_ok,
                "value_ok": value_ok,
                "evidence_ok": evidence_ok,
            }
        )

    n = len(items)
    return WhitepaperScorecard(
        n=n,
        recall_at_k=(value_correct / value_checked) if value_checked else 1.0,
        citation_precision=(evidence_correct / evidence_checked) if evidence_checked else 1.0,
        refusal_accuracy=status_correct / n,
        status_correct=status_correct,
        value_checked=value_checked,
        value_correct=value_correct,
        evidence_checked=evidence_checked,
        evidence_correct=evidence_correct,
        details=details,
    )
