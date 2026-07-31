"""Eval runner.

Reads `gold_set.jsonl`, runs each question through `grounded_qa.ask`, and
prints a scorecard. With `--check-thresholds`, exits non-zero if any metric
is below the spec §12 targets.

Targets (POC):
  recall@8           ≥ 0.90
  citation_precision ≥ 0.95
  refusal_accuracy   ≥ 0.95
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from regwatch.eval.metrics import GoldItem, Scorecard, evaluate
from regwatch.generate.grounded_qa import ask
from regwatch.generate.prompts import generation_prompt_manifest
from regwatch.store.db import init_db
from regwatch.store.vector_store import collection_size

app = typer.Typer(
    no_args_is_help=False, add_completion=False, help="Evaluate REGWATCH on a gold set."
)


THRESHOLDS = {
    "recall_at_k": 0.90,
    "citation_precision": 0.95,
    "refusal_accuracy": 0.95,
}


def _load_gold(path: Path) -> list[GoldItem]:
    items: list[GoldItem] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            items.append(
                GoldItem(
                    question=row["question"],
                    expected_sources=row.get("expected_sources") or [],
                    expected_facts=row.get("expected_facts") or [],
                    must_refuse=bool(row.get("must_refuse", False)),
                    must_clarify=bool(row.get("must_clarify", False)),
                )
            )
    return items


def _print_scorecard(sc: Scorecard) -> None:
    console = Console()
    table = Table(title="REGWATCH eval scorecard")
    table.add_column("metric", style="bold")
    table.add_column("value", justify="right")
    table.add_column("threshold", justify="right")
    table.add_column("status")
    for key in (
        "recall_at_k",
        "citation_precision",
        "faithfulness",
        "fact_recall",
        "refusal_accuracy",
    ):
        v = getattr(sc, key)
        thr = THRESHOLDS.get(key)
        status = "—"
        if thr is not None:
            status = "[green]ok[/green]" if v >= thr else "[red]FAIL[/red]"
        table.add_row(key, f"{v:.3f}", f"{thr:.2f}" if thr is not None else "—", status)
    console.print(table)
    console.print(
        f"refused_correctly={sc.refused_correctly}  "
        f"clarified_correctly={sc.clarified_correctly}  "
        f"refused_incorrectly={sc.refused_incorrectly}  "
        f"cited_ungrounded={sc.cited_ungrounded}"
    )
    if sc.skipped:
        # Loud, explicit — never a silent pass. These items asserted multi-form
        # clarify behavior on a product the seeded corpus does not contain.
        absent = [d["q"] for d in sc.details if d.get("skipped")]
        console.print(
            f"[yellow]skipped {sc.skipped} must_clarify item(s) absent from the "
            f"seeded corpus (excluded from refusal_accuracy; still hard-gated "
            f"offline in the deterministic eval gate): {absent}[/yellow]"
        )


@app.command()
def run(
    gold: Path = typer.Option(
        Path(__file__).parent / "gold_set.jsonl",
        "--gold",
        help="Path to JSONL gold set",
    ),
    check_thresholds: bool = typer.Option(
        False,
        "--check-thresholds",
        help="Exit non-zero if any metric is below the spec §12 target.",
    ),
    out: Path | None = typer.Option(None, "--out", help="Write scorecard JSON to this path."),
) -> None:
    init_db()
    if collection_size() == 0:
        Console().print(
            "[yellow]Vector store is empty — no eval possible. "
            "Run `uv run regwatch seed` first.[/yellow]"
        )
        # A gating run must NOT pass silently on an unseeded store; only a
        # non-gating (observability) run exits clean.
        if check_thresholds:
            sys.exit(2)
        return

    items = _load_gold(gold)
    sc = evaluate(items, ask_callable=ask)
    _print_scorecard(sc)
    if out:
        artifact = asdict(sc)
        artifact["artifact_schema_version"] = 2
        artifact["prompts"] = generation_prompt_manifest()
        out.write_text(json.dumps(artifact, indent=2))

    if check_thresholds:
        violations = [
            (k, getattr(sc, k), thr) for k, thr in THRESHOLDS.items() if getattr(sc, k) < thr
        ]
        if violations:
            sys.exit(2)


if __name__ == "__main__":
    app()
