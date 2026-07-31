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
import os
import sys
from dataclasses import asdict
from pathlib import Path

import typer
from config.settings import get_settings
from rich.console import Console
from rich.table import Table

from regwatch.eval import run_fingerprint
from regwatch.eval.metrics import GoldItem, Scorecard, evaluate
from regwatch.generate.grounded_qa import ask
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


def _apply_profile(profile: str) -> str:
    """Point THIS PROCESS at one embedding arm, then re-read settings.

    Retrieval already selects its arm from ACTIVE_EMBEDDING_PROFILE
    (retrieve/retriever.py), so an A/B needs no second retrieval path -- only a
    process configured for one arm. One arm per process is the point: two arms
    in one interpreter would share the settings cache, the provider clients and
    any warm state, and the second run would no longer be independent.
    """
    profile = (profile or run_fingerprint.LEGACY).strip()
    if profile != run_fingerprint.LEGACY:
        # Check the shape here, where it is still a CLI argument. init_db
        # validates it too, but by then a typo surfaces as a boot traceback
        # instead of a message about the flag you just typed.
        from regwatch.store.embedding_profiles import _validate_profile_id

        try:
            _validate_profile_id(profile)
        except ValueError as exc:
            raise SystemExit(f"--profile {profile!r} is not a usable arm: {exc}") from exc
    os.environ["ACTIVE_EMBEDDING_PROFILE"] = profile
    get_settings.cache_clear()
    s = get_settings()
    resolved = (s.active_embedding_profile or run_fingerprint.LEGACY).strip()
    if resolved != profile:
        raise SystemExit(
            f"--profile {profile!r} did not take effect (settings report {resolved!r})"
        )
    return profile


def _assert_profile_ready(profile: str) -> None:
    """DB-side readiness. Separate from _apply_profile because that one must run
    before init_db (it decides configuration) while this one needs the engine."""
    if profile == run_fingerprint.LEGACY:
        return
    # Fail before spending a single LLM call on an arm that cannot serve every
    # question: partial coverage silently degrades recall instead of erroring.
    from regwatch.store.embedding_profiles import (
        profile_embedding_coverage,
        profile_hnsw_index_ready,
    )

    try:
        coverage = profile_embedding_coverage(profile)
        # Index readiness is its own probe, NOT a field on coverage. Reading it
        # off the dataclass with a getattr default silently answered "no index"
        # for every profile, which rejected every non-legacy arm outright.
        index_ready = profile_hnsw_index_ready(profile)
    except (ValueError, LookupError) as exc:
        # A typo'd or unregistered arm is operator error, not a crash: the id
        # guard and the missing-row lookup both deserve one readable line.
        raise SystemExit(f"--profile {profile!r} is not a usable arm: {exc}") from exc
    if not coverage.complete:
        raise SystemExit(
            f"profile {profile} is not fully embedded ({coverage.pending_chunks} "
            "chunk(s) pending); backfill before evaluating it"
        )
    if not index_ready:
        raise SystemExit(
            f"profile {profile} has no ready HNSW index; run `regwatch "
            f"embedding-profile-index {profile}` before evaluating it"
        )


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


def _print_fingerprint(fp: run_fingerprint.RunFingerprint) -> None:
    console = Console()
    detail = fp.profile_detail
    console.print(
        f"[dim]arm={fp.profile} model={detail.get('model') or '?'} "
        f"corpus={fp.corpus.chunks} chunks/{fp.corpus.docs} docs "
        f"digest={fp.corpus.digest[:12] or '?'} "
        f"vector_top_k={fp.retrieval.get('vector_top_k')} "
        f"rerank_top_k={fp.retrieval.get('rerank_top_k')} "
        f"reranker={'on' if fp.retrieval.get('reranker_enabled') else 'off'} "
        f"llm={fp.models.get('llm_model')} commit={fp.commit[:8]}"
        f"{' [yellow](dirty tree)[/yellow]' if fp.dirty else ''}[/dim]"
    )
    if fp.dirty:
        # Two arms compared across a dirty tree may not have run the same code.
        console.print(
            "[yellow]working tree has uncommitted tracked changes: this run is "
            "not reproducible from its commit alone[/yellow]"
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
    profile: str = typer.Option(
        run_fingerprint.LEGACY,
        "--profile",
        help=(
            "Embedding arm for this run: 'legacy' (chunk.embedding column) or a "
            "registered profile id. Applies to this process only -- run each arm "
            "in its own invocation so the two are independent."
        ),
    ),
) -> None:
    profile = _apply_profile(profile)
    try:
        init_db()
    except KeyError as exc:
        # init_db resolves the active arm (dimension fail-fast), so an
        # unregistered id is caught here rather than by _assert_profile_ready
        # below. KeyError from this call means exactly one thing -- the profile
        # row does not exist -- so translating it cannot mask a boot failure.
        raise SystemExit(f"--profile {profile!r} is not a usable arm: {exc}") from exc
    _assert_profile_ready(profile)
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
    # Built before the run so the artifact describes the configuration the
    # questions actually executed under, not one a later flip could rewrite.
    fingerprint = run_fingerprint.build(profile, THRESHOLDS)
    sc = evaluate(items, ask_callable=ask)
    _print_scorecard(sc)
    _print_fingerprint(fingerprint)
    if out:
        out.write_text(
            json.dumps(
                {"fingerprint": fingerprint.to_dict(), "scorecard": asdict(sc)},
                indent=2,
                default=str,
            )
        )

    if check_thresholds:
        violations = [
            (k, getattr(sc, k), thr) for k, thr in THRESHOLDS.items() if getattr(sc, k) < thr
        ]
        if violations:
            sys.exit(2)


if __name__ == "__main__":
    app()
