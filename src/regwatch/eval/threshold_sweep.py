"""Refusal-threshold REVALIDATION harness (observability / advisory only).

Why this exists
---------------
``config.settings.Settings.refusal_score_threshold`` (default ``0.30``) gates the
``low_top_score`` refusal in ``grounded_qa.ask`` (grounded_qa.py: the INV-2 gate
``max(p.score for p in passages) < refusal_score_threshold``). That ``0.30`` was
calibrated in the bge-384 cosine era. Production has embedded with the Databricks
Qwen3 profile since 2026-07-30 (1024 dims, endpoint workspace.default.regwatch-embed),
a DIFFERENT vector space with a DIFFERENT cosine-similarity distribution. The
deterministic CI retrieval fixture does not re-validate that live distribution;
only a provider-backed run, such as the credentialed watch-daily job, can do so.

This harness dumps the per-query max-passage cosine score distribution across the
gold set, split into must-answer vs must-refuse, and RECOMMENDS a cutoff. It is
strictly READ-ONLY w.r.t. the safety path: it never imports-to-mutate, never
changes ``0.30``, and (by itself) exits 0 even when its recommendation differs
from the live value. It reports; humans decide.

It reuses the exact prod retrieval path by calling the real ``grounded_qa.ask``
over the small gold set. Each ``ask`` is one real retrieval plus
one cheap LLM synthesis (gpt-5.4-nano) — acceptable cost for an advisory sweep,
and the only way to read scores off the REAL prod embedding space rather than a
re-implementation that could drift from the gate. The score read is independent
of the ``0.30`` gate: ``ask`` populates ``result.retrieved`` (each row carrying a
numeric ``"score"``) even on the ``low_top_score`` refusal path, so the pre-gate
max passage cosine is observable for every question where retrieval actually ran.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from regwatch.eval.metrics import GoldItem, recall_at_k

# Default sweep grid: a fine 0.01 step over the full normalized [0, 1] score
# range. 0.30 (the current default)
# lands exactly on the grid, so the recommendation and the current value are
# compared like-for-like.
_DEFAULT_STEP = 0.01
_DEFAULT_HI = 1.00
_CURRENT_DEFAULT = 0.30


def default_candidates(*, step: float = _DEFAULT_STEP, hi: float = _DEFAULT_HI) -> list[float]:
    """Inclusive grid [0, hi] at ``step``, rounded to avoid float drift on the keys."""
    # round() with no ndigits returns an int; the +1 makes the grid inclusive of hi.
    n = round(hi / step)
    return [round(i * step, 4) for i in range(n + 1)]


@dataclass
class ScoreRow:
    """One gold item's observed retrieval outcome, threshold-INDEPENDENT.

    ``max_score`` is the pre-gate max passage cosine, or ``None`` when retrieval
    never ran (the resolver refused before retrieval — e.g. reason ``no_product``
    or requested clarification — leaving ``result.retrieved == []``). A ``None``
    score is threshold-independent; its observed ``refused`` value applies at
    every candidate cutoff.
    """

    question: str
    must_refuse: bool
    # Clarification behavior is a resolver decision, not a numeric cosine-
    # threshold decision. Keep it in the artifact for auditability, but exclude
    # it from both sides of the threshold curve.
    must_clarify: bool
    max_score: float | None
    recall_hit: bool
    refused: bool
    status: str | None
    reason: str | None
    n_retrieved: int


def collect_scores(
    items: Sequence[GoldItem],
    *,
    ask_callable: Callable[[str], Any],
) -> list[ScoreRow]:
    """Run every gold item through ``ask_callable`` and read off the score row.

    ``ask_callable`` defaults to the REAL ``grounded_qa.ask`` (see ``_real_ask``)
    so the sweep observes the exact prod retrieval path; tests inject a stub. The
    score is read off ``result.retrieved`` — populated even on the low_top_score
    refusal path — so ``max_score`` is the true pre-gate best cosine, independent
    of the ``0.30`` decision (the gate flips ``refused``; it does not touch the
    scores in ``retrieved``).
    """
    rows: list[ScoreRow] = []
    for it in items:
        result = ask_callable(it.question)
        retrieved = list(getattr(result, "retrieved", []) or [])
        # max(score) over retrieved, or None when retrieval never ran. We tolerate
        # rows missing a "score" key defensively (None-skip), though the prod
        # _audit_retrieved always carries one.
        scores = [p["score"] for p in retrieved if p.get("score") is not None]
        max_score = max(scores) if scores else None
        rows.append(
            ScoreRow(
                question=it.question,
                must_refuse=bool(it.must_refuse),
                must_clarify=bool(it.must_clarify),
                max_score=max_score,
                recall_hit=recall_at_k(retrieved, it.expected_sources) == 1,
                refused=bool(getattr(result, "refused", False)),
                status=getattr(result, "status", None),
                reason=getattr(result, "reason", None),
                n_retrieved=len(retrieved),
            )
        )
    return rows


def _would_refuse(row: ScoreRow, t: float) -> bool:
    """The decision at threshold ``t``.

    A scored row follows the numeric gate. When retrieval never ran, preserve
    the observed resolver/scope outcome; a clarification must not be silently
    relabeled as a refusal.
    """
    if row.max_score is None:
        return row.refused
    return row.max_score < t


def _would_answer(row: ScoreRow, t: float) -> bool:
    return not _would_refuse(row, t)


@dataclass
class DistStats:
    """min / median / max of a max-score distribution (None-scores excluded)."""

    n: int
    n_scored: int  # rows with a non-None max_score
    min: float | None
    median: float | None
    max: float | None


def _dist_stats(rows: Sequence[ScoreRow]) -> DistStats:
    scores = [r.max_score for r in rows if r.max_score is not None]
    if not scores:
        return DistStats(n=len(rows), n_scored=0, min=None, median=None, max=None)
    return DistStats(
        n=len(rows),
        n_scored=len(scores),
        min=min(scores),
        median=float(median(scores)),
        max=max(scores),
    )


@dataclass
class CurvePoint:
    threshold: float
    refuse_recall: float  # fraction of must_refuse rows that WOULD refuse at t
    answer_retention: float  # fraction of must-answer rows that WOULD answer at t
    decision_accuracy: float  # (correct refuse + correct answer) / n


@dataclass
class SweepResult:
    curve: list[CurvePoint] = field(default_factory=list)
    must_answer_stats: DistStats | None = None
    must_refuse_stats: DistStats | None = None
    n_must_answer: int = 0
    n_must_refuse: int = 0
    n_must_clarify: int = 0


def sweep(
    rows: Sequence[ScoreRow],
    *,
    candidates: Sequence[float] | None = None,
) -> SweepResult:
    """Compute the full decision curve over ``candidates`` plus distribution stats.

    For each candidate ``t``:
      refuse_recall(t)     = fraction of must_refuse rows that WOULD refuse at t
                             (observed pre-retrieval refusal OR max_score < t)
      answer_retention(t)  = fraction of must-answer rows that WOULD answer at t
                             (max_score is not None AND max_score >= t)
      decision_accuracy(t) = (correct refusals + correct answers) / n

    ``must_clarify`` rows are excluded. They test resolver behavior and do not
    supply a positive or negative cosine score for threshold calibration.
    """
    cands = list(candidates) if candidates is not None else default_candidates()
    must_clarify = [r for r in rows if r.must_clarify]
    must_refuse = [r for r in rows if r.must_refuse and not r.must_clarify]
    must_answer = [r for r in rows if not r.must_refuse and not r.must_clarify]
    n = len(must_refuse) + len(must_answer)

    curve: list[CurvePoint] = []
    for t in cands:
        refuse_recall = (
            sum(1 for r in must_refuse if _would_refuse(r, t)) / len(must_refuse)
            if must_refuse
            else 1.0
        )
        answer_retention = (
            sum(1 for r in must_answer if _would_answer(r, t)) / len(must_answer)
            if must_answer
            else 1.0
        )
        # decision_accuracy over the FULL set: a must_refuse is correct when it
        # would refuse; a must-answer is correct when it would answer.
        correct = sum(1 for r in must_refuse if _would_refuse(r, t)) + sum(
            1 for r in must_answer if _would_answer(r, t)
        )
        decision_accuracy = correct / n if n else 1.0
        curve.append(
            CurvePoint(
                threshold=t,
                refuse_recall=refuse_recall,
                answer_retention=answer_retention,
                decision_accuracy=decision_accuracy,
            )
        )

    return SweepResult(
        curve=curve,
        must_answer_stats=_dist_stats(must_answer),
        must_refuse_stats=_dist_stats(must_refuse),
        n_must_answer=len(must_answer),
        n_must_refuse=len(must_refuse),
        n_must_clarify=len(must_clarify),
    )


@dataclass
class Recommendation:
    recommended: float | None
    current: float
    rationale: str
    provisional: bool  # True when the distributions overlap (no clean separator)
    overlap: bool
    # Curve metrics AT the current 0.30 cutoff (the baseline humans compare to).
    current_refuse_recall: float
    current_answer_retention: float
    current_decision_accuracy: float
    # Curve metrics AT the recommended cutoff.
    recommended_refuse_recall: float | None
    recommended_answer_retention: float | None
    recommended_decision_accuracy: float | None
    # Pathology (a): must-answer rows already wrongly refused at current.
    wrongly_refused_at_current: list[str] = field(default_factory=list)
    # Pathology (b): must-refuse rows already leaking through (answered) at current.
    leaking_at_current: list[str] = field(default_factory=list)


def _point_at(curve: Sequence[CurvePoint], t: float) -> CurvePoint | None:
    if not curve:
        return None
    return min(curve, key=lambda p: abs(p.threshold - t))


def recommend(
    rows: Sequence[ScoreRow],
    sweep_result: SweepResult,
    *,
    current: float = _CURRENT_DEFAULT,
) -> Recommendation:
    """Recommend a cutoff that MAXIMIZES refuse_recall WITHOUT refusing anything
    currently answered.

    Constraint: answer_retention(t) >= answer_retention(current). Among candidates
    that satisfy it, pick the one with the highest refuse_recall; break ties toward
    the LOWER threshold (the least aggressive change that achieves the gain). This
    can never refuse an item the live 0.30 currently answers.

    Also flags two standing pathologies AT the current 0.30 cutoff:
      (a) must-answer rows with max_score < current — already wrongly refused.
      (b) must-refuse rows with max_score >= current — already leaking through.

    If the must-answer and must-refuse score distributions OVERLAP (the lowest
    must-answer max_score is below the highest must-refuse max_score), there is no
    clean separator: the recommendation is the best available tradeoff and is
    labelled provisional.
    """
    curve = sweep_result.curve
    must_refuse = [r for r in rows if r.must_refuse and not r.must_clarify]
    must_answer = [r for r in rows if not r.must_refuse and not r.must_clarify]

    cur_point = _point_at(curve, current)
    cur_retention = cur_point.answer_retention if cur_point else 1.0
    cur_refuse_recall = cur_point.refuse_recall if cur_point else 1.0
    cur_decision_acc = cur_point.decision_accuracy if cur_point else 1.0

    # Pathology flags computed directly off the rows at the LITERAL current value
    # (not the nearest grid point) so they are exact regardless of grid choice.
    wrongly_refused = [
        r.question for r in must_answer if r.max_score is not None and r.max_score < current
    ]
    leaking = [
        r.question for r in must_refuse if r.max_score is not None and r.max_score >= current
    ]

    # Overlap test on the two distributions (None scores excluded — a None
    # must-refuse row refuses at every t and never causes overlap).
    ma_scores = [r.max_score for r in must_answer if r.max_score is not None]
    mr_scores = [r.max_score for r in must_refuse if r.max_score is not None]
    overlap = bool(ma_scores) and bool(mr_scores) and (min(ma_scores) <= max(mr_scores))

    # A numeric cutoff cannot be calibrated without scored examples on both
    # sides. Resolver/scope refusals with max_score=None are safety evidence, but
    # they reveal nothing about where the cosine threshold should sit.
    if not ma_scores or not mr_scores:
        missing_groups: list[str] = []
        if not ma_scores:
            missing_groups.append("must-answer")
        if not mr_scores:
            missing_groups.append("must-refuse")
        missing = " and ".join(missing_groups)
        rationale = (
            f"Cannot calibrate a cosine cutoff: no scored {missing} rows reached "
            "vector retrieval. Pre-retrieval resolver or scope decisions do not "
            "establish separation in the embedding score space."
        )
        return Recommendation(
            recommended=None,
            current=current,
            rationale=rationale,
            provisional=True,
            overlap=False,
            current_refuse_recall=cur_refuse_recall,
            current_answer_retention=cur_retention,
            current_decision_accuracy=cur_decision_acc,
            recommended_refuse_recall=None,
            recommended_answer_retention=None,
            recommended_decision_accuracy=None,
            wrongly_refused_at_current=wrongly_refused,
            leaking_at_current=leaking,
        )

    # Feasible set: candidates that do NOT lose any currently-answered item.
    feasible = [p for p in curve if p.answer_retention >= cur_retention]
    best: CurvePoint | None = None
    if feasible:
        # Maximize refuse_recall; tie-break toward the lower threshold.
        best = max(feasible, key=lambda p: (p.refuse_recall, -p.threshold))

    if best is None:
        rationale = (
            "No candidate satisfies the retention floor "
            f"(answer_retention >= {cur_retention:.3f}); cannot recommend a "
            "change without refusing a currently-answered item."
        )
        return Recommendation(
            recommended=None,
            current=current,
            rationale=rationale,
            provisional=True,
            overlap=overlap,
            current_refuse_recall=cur_refuse_recall,
            current_answer_retention=cur_retention,
            current_decision_accuracy=cur_decision_acc,
            recommended_refuse_recall=None,
            recommended_answer_retention=None,
            recommended_decision_accuracy=None,
            wrongly_refused_at_current=wrongly_refused,
            leaking_at_current=leaking,
        )

    if overlap:
        rationale = (
            "Distributions OVERLAP (a must-refuse item scores at or above a "
            "must-answer item) — no clean separator. Recommending the best "
            f"retention-preserving tradeoff t={best.threshold:.2f} "
            f"(refuse_recall={best.refuse_recall:.3f}, "
            f"answer_retention={best.answer_retention:.3f}). PROVISIONAL: a "
            "perfect cutoff does not exist; some leakage or over-refusal is "
            "unavoidable without fixing retrieval."
        )
    else:
        rationale = (
            f"Recommend t={best.threshold:.2f}: maximizes refuse_recall "
            f"({best.refuse_recall:.3f}) while keeping answer_retention "
            f"({best.answer_retention:.3f}) >= current "
            f"({cur_retention:.3f}). Distributions are cleanly separable."
        )

    return Recommendation(
        recommended=best.threshold,
        current=current,
        rationale=rationale,
        provisional=overlap,
        overlap=overlap,
        current_refuse_recall=cur_refuse_recall,
        current_answer_retention=cur_retention,
        current_decision_accuracy=cur_decision_acc,
        recommended_refuse_recall=best.refuse_recall,
        recommended_answer_retention=best.answer_retention,
        recommended_decision_accuracy=best.decision_accuracy,
        wrongly_refused_at_current=wrongly_refused,
        leaking_at_current=leaking,
    )


# ---------------------------------------------------------------------------
# CLI — its OWN typer app (must NOT touch run_eval's app / its single `run`
# command, which CI invokes as `python -m regwatch.eval.run_eval`).
# ---------------------------------------------------------------------------


def _real_ask(question: str) -> Any:
    """Default ask_callable: the REAL prod retrieval+synthesis path. Imported
    lazily so importing this module (e.g. in unit tests with a stub) never drags
    in the LLM/DB stack."""
    from regwatch.generate.grounded_qa import ask

    return ask(question)


def _serialize(
    rows: Sequence[ScoreRow],
    sweep_result: SweepResult,
    rec: Recommendation,
) -> dict[str, Any]:
    return {
        "rows": [asdict(r) for r in rows],
        "curve": [asdict(p) for p in sweep_result.curve],
        "distributions": {
            "must_answer": (
                asdict(sweep_result.must_answer_stats) if sweep_result.must_answer_stats else None
            ),
            "must_refuse": (
                asdict(sweep_result.must_refuse_stats) if sweep_result.must_refuse_stats else None
            ),
        },
        "counts": {
            "must_answer": sweep_result.n_must_answer,
            "must_refuse": sweep_result.n_must_refuse,
            "must_clarify_excluded": sweep_result.n_must_clarify,
        },
        "recommendation": asdict(rec),
    }


def _print_scorecard(
    sweep_result: SweepResult,
    rec: Recommendation,
) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()

    dist = Table(title="Refusal-threshold revalidation — score distributions")
    dist.add_column("group", style="bold")
    dist.add_column("n", justify="right")
    dist.add_column("scored", justify="right")
    dist.add_column("min", justify="right")
    dist.add_column("median", justify="right")
    dist.add_column("max", justify="right")

    def _fmt(x: float | None) -> str:
        return f"{x:.3f}" if x is not None else "—"

    for label, st in (
        ("must-answer", sweep_result.must_answer_stats),
        ("must-refuse", sweep_result.must_refuse_stats),
    ):
        if st is None:
            dist.add_row(label, "0", "0", "—", "—", "—")
            continue
        dist.add_row(
            label,
            str(st.n),
            str(st.n_scored),
            _fmt(st.min),
            _fmt(st.median),
            _fmt(st.max),
        )
    console.print(dist)

    rec_tbl = Table(title="Recommendation (ADVISORY — does not change settings)")
    rec_tbl.add_column("cutoff", style="bold")
    rec_tbl.add_column("refuse_recall", justify="right")
    rec_tbl.add_column("answer_retention", justify="right")
    rec_tbl.add_column("decision_accuracy", justify="right")
    rec_tbl.add_row(
        f"current={rec.current:.2f}",
        _fmt(rec.current_refuse_recall),
        _fmt(rec.current_answer_retention),
        _fmt(rec.current_decision_accuracy),
    )
    rec_tbl.add_row(
        (
            f"recommended={rec.recommended:.2f}"
            if rec.recommended is not None
            else "recommended=NONE"
        ),
        _fmt(rec.recommended_refuse_recall),
        _fmt(rec.recommended_answer_retention),
        _fmt(rec.recommended_decision_accuracy),
    )
    console.print(rec_tbl)

    tag = "[yellow]PROVISIONAL[/yellow]" if rec.provisional else "[green]clean[/green]"
    console.print(f"{tag} {rec.rationale}")

    if rec.wrongly_refused_at_current:
        console.print(
            f"[red]PATHOLOGY (a) — {len(rec.wrongly_refused_at_current)} must-answer "
            f"item(s) ALREADY wrongly refused at {rec.current:.2f} "
            f"(max_score < {rec.current:.2f}):[/red] {rec.wrongly_refused_at_current}"
        )
    if rec.leaking_at_current:
        console.print(
            f"[red]PATHOLOGY (b) — {len(rec.leaking_at_current)} must-refuse item(s) "
            f"ALREADY leaking through at {rec.current:.2f} "
            f"(max_score >= {rec.current:.2f}):[/red] {rec.leaking_at_current}"
        )
    if not rec.wrongly_refused_at_current and not rec.leaking_at_current:
        console.print(
            f"[green]No standing pathologies at the current {rec.current:.2f} " "cutoff.[/green]"
        )
    console.print("[dim]Advisory only: 0.30 is unchanged. A human revalidates and decides.[/dim]")


def _build_app() -> Any:
    import sys

    import typer

    app = typer.Typer(
        no_args_is_help=False,
        add_completion=False,
        help=(
            "Revalidate the refusal score threshold against the gold set in the "
            "CURRENT embedding space. Advisory: reports a recommended cutoff and "
            "never changes settings."
        ),
    )

    @app.command()
    def sweep_cmd(
        gold: Path = typer.Option(
            Path(__file__).parent / "gold_set.jsonl",
            "--gold",
            help="Path to JSONL gold set.",
        ),
        out: Path | None = typer.Option(
            None,
            "--out",
            help="Write the full JSON (rows + curve + recommendation) to this path.",
        ),
        current: float = typer.Option(
            _CURRENT_DEFAULT,
            "--current",
            help="The live threshold to compare against (default 0.30). Reporting only.",
        ),
    ) -> None:
        # Imported here (not at module top) so unit tests never touch the DB.
        from rich.console import Console

        from regwatch.eval.run_eval import _load_gold
        from regwatch.store.db import init_db
        from regwatch.store.vector_store import collection_size

        init_db()
        # Mirror run_eval: bail LOUDLY on an empty store. A revalidation against an
        # empty index would silently "recommend" off zero data — refuse to run.
        if collection_size() == 0:
            Console().print(
                "[red]Vector store is EMPTY — cannot revalidate the threshold. "
                "Seed the prod embedding space first (e.g. `regwatch seed` or the "
                "watch-daily ingest with EMBEDDING_PROVIDER=openai).[/red]"
            )
            raise typer.Exit(code=2)

        items = _load_gold(gold)
        rows = collect_scores(items, ask_callable=_real_ask)
        sweep_result = sweep(rows)
        rec = recommend(rows, sweep_result, current=current)

        _print_scorecard(sweep_result, rec)

        if out is not None:
            out.write_text(json.dumps(_serialize(rows, sweep_result, rec), indent=2))
            Console().print(f"[dim]Wrote full sweep JSON to {out}[/dim]")

        # Advisory by design: exit 0 even when the recommendation != current. This
        # tool reports; it does not gate. (Use a non-zero exit ONLY for the
        # operational failures above: empty store.)
        sys.exit(0)

    return app


app = _build_app()


if __name__ == "__main__":
    app()
