"""Raw embedding diagnostic: legacy vector column vs a named embedding profile.

NOT A RELEASE GATE, and deliberately not production-equivalent. This measures
embedding geometry in isolation: a bare vector search at a fixed k, with no
product/form resolution, no current-version scoping, and no reranking.

Production retrieval does none of that in isolation. It resolves the product
and dosage form into filters, restricts to current versions, pulls a wide net
of VECTOR_TOP_K (50) candidates, then reranks and trims to RERANK_TOP_K (8)
before anything is cited (retrieve/retriever.py, grounded_qa.py). Those filters
change the outcome materially -- on 2026-07-31 the unfiltered comparison here
found the expected page for 4 of 6 gold items in BOTH arms while the filtered
production path scored recall 1.000 in both. So a regression printed here is a
hypothesis about embedding geometry, never a verdict about the product.

The release gate is `run_eval --profile <arm>`, which runs the real pipeline
end to end and records a per-question trace and a run fingerprint. Use this
tool to explain a result that gate produced, not to decide a flip.

Compares:

  - hit_rate@k / MRR on the human gold set, page-level match (short_name, page)
  - hit_rate@k / MRR on an auto-generated doc-level set (question templated
    from structured metadata only -- never chunk text, so neither embedding
    arm sees its own corpus phrasing in the query)
  - top-1 score distribution on must_refuse items vs answerable items, next
    to REFUSAL_SCORE_THRESHOLD (the 0.30 threshold was calibrated on legacy
    geometry; this shows whether the profile's geometry moves it)
  - per-query embed and search latency p50/p95, for UNFILTERED k queries;
    production issues a filtered k=50 query against a partial index, which is a
    different plan, so these numbers do not predict production latency

Exit codes report whether the diagnostic could run (0 = ran, non-zero =
operational failure such as an empty corpus or a run that measured nothing).
They never encode a quality judgement, so nothing can gate on them.

Run:
  uv run python -m regwatch.eval.embedding_benchmark --profile <profile_id>
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from regwatch.eval.metrics import _match_source

app = typer.Typer(no_args_is_help=False, add_completion=False)


# ---------------------------------------------------------------------------
# Pure scoring helpers (unit-tested without a DB)
# ---------------------------------------------------------------------------


def rank_of_first_match(
    hit_metas: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> int | None:
    """1-based rank of the first retrieved hit matching an expected source."""
    for i, meta in enumerate(hit_metas, start=1):
        if _match_source(meta, expected):
            return i
    return None


def doc_level_rank(hit_metas: list[dict[str, Any]], short_names: set[str]) -> int | None:
    """1-based rank of the first hit whose short_name is in the expected set."""
    for i, meta in enumerate(hit_metas, start=1):
        if meta.get("short_name") in short_names:
            return i
    return None


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list (reported, not hidden)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


@dataclass
class ArmResult:
    name: str
    ranks: list[int | None] = field(default_factory=list)
    top_scores: list[float] = field(default_factory=list)
    refuse_top_scores: list[float] = field(default_factory=list)
    doc_ranks: list[int | None] = field(default_factory=list)
    embed_latencies: list[float] = field(default_factory=list)
    search_latencies: list[float] = field(default_factory=list)

    @staticmethod
    def _hit_rate(ranks: list[int | None]) -> float:
        return sum(1 for r in ranks if r is not None) / len(ranks) if ranks else 0.0

    @staticmethod
    def _mrr(ranks: list[int | None]) -> float:
        return sum(1.0 / r for r in ranks if r is not None) / len(ranks) if ranks else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "arm": self.name,
            "gold_n": len(self.ranks),
            "gold_hit_rate": self._hit_rate(self.ranks),
            "gold_mrr": self._mrr(self.ranks),
            "doc_n": len(self.doc_ranks),
            "doc_hit_rate": self._hit_rate(self.doc_ranks),
            "doc_mrr": self._mrr(self.doc_ranks),
            "answerable_top_score_mean": (
                statistics.fmean(self.top_scores) if self.top_scores else 0.0
            ),
            "answerable_top_score_min": min(self.top_scores, default=0.0),
            "refuse_top_score_mean": (
                statistics.fmean(self.refuse_top_scores) if self.refuse_top_scores else 0.0
            ),
            "refuse_top_score_max": max(self.refuse_top_scores, default=0.0),
            "embed_p50_ms": percentile(self.embed_latencies, 50) * 1000,
            "embed_p95_ms": percentile(self.embed_latencies, 95) * 1000,
            "search_p50_ms": percentile(self.search_latencies, 50) * 1000,
            "search_p95_ms": percentile(self.search_latencies, 95) * 1000,
        }


@dataclass
class Comparison:
    """What moved between the arms, and whether anything was measured at all.

    `degenerate` is the important field: when nothing was retrieved, every delta
    is 0, which reads identically to "no change". That is why this is reported
    rather than folded into a pass/fail -- a zero delta over zero evidence is
    not a result.
    """

    observations: list[str] = field(default_factory=list)
    degenerate: bool = False
    degenerate_reason: str = ""


def compare_arms(legacy: dict[str, Any], profile: dict[str, Any]) -> Comparison:
    """Describe how the profile arm differs from the legacy arm.

    Reports; does not judge. A metric moving here says something about embedding
    geometry and nothing about whether the product got worse -- production
    filters and reranks before any of this reaches an answer.
    """
    observations: list[str] = []
    checks = (
        ("gold_hit_rate", "gold_n"),
        ("gold_mrr", "gold_n"),
        ("doc_hit_rate", "doc_n"),
        ("doc_mrr", "doc_n"),
    )
    # Degenerate runs first. A wiped or partial corpus, a gold set whose pages
    # went stale after a re-chunk, or --doc-limit 0 with a refuse-only gold file
    # all drive every delta to 0, which is indistinguishable from "unchanged".
    if int(legacy.get("gold_n") or 0) == 0 and int(legacy.get("doc_n") or 0) == 0:
        return Comparison(
            degenerate=True,
            degenerate_reason="no questions evaluated: this run measured nothing",
        )
    if float(legacy.get("gold_hit_rate") or 0.0) == 0.0 and (
        float(legacy.get("doc_hit_rate") or 0.0) == 0.0
    ):
        return Comparison(
            degenerate=True,
            degenerate_reason=(
                "the legacy baseline retrieved nothing on every question set: the "
                "corpus or the expected sources are wrong, so the deltas are noise"
            ),
        )
    for metric, n_key in checks:
        n = int(legacy.get(n_key) or 0)
        if n == 0:
            continue
        # One item's worth of swing; 1e-9 absorbs float rounding so an
        # exactly-one-item move is not reported as more than one item.
        noise = 1.0 / n
        delta = float(profile[metric]) - float(legacy[metric])
        if abs(delta) > noise + 1e-9:
            direction = "lower" if delta < 0 else "higher"
            observations.append(
                f"{metric}: profile {profile[metric]:.3f} vs legacy "
                f"{legacy[metric]:.3f} ({direction} by more than 1/{n})"
            )
    return Comparison(observations=observations)


# ---------------------------------------------------------------------------
# Question sets
# ---------------------------------------------------------------------------


def load_gold_items(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """(answerable items with expected_sources, must_refuse questions)."""
    answerable: list[dict[str, Any]] = []
    refuse: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            if row.get("must_refuse"):
                refuse.append(row["question"])
            elif row.get("must_clarify"):
                continue  # asserts a routing decision, not retrieval quality
            elif row.get("expected_sources"):
                answerable.append(row)
    return answerable, refuse


_DOC_TEMPLATES = (
    "What study design does the PSG for {name} {form} recommend?",
    "What bioequivalence approach does FDA recommend for {name} ({route} {form})?",
    "Summarize the product-specific guidance recommendations for {name} {form}.",
)


def build_doc_items(limit: int) -> list[dict[str, Any]]:
    """Doc-level items from structured metadata (never chunk text).

    One item per distinct (normalized_name, dosage_form, route); expected =
    every short_name that combo's chunks carry (any of them counts as a hit).
    Deterministic order so reruns are comparable.
    """
    from sqlalchemy import text as sa_text

    from regwatch.store.db import get_engine

    sql = (
        "SELECT normalized_name, dosage_form, route, "
        "array_agg(DISTINCT short_name) AS short_names "
        "FROM chunk WHERE normalized_name IS NOT NULL AND short_name IS NOT NULL "
        "GROUP BY normalized_name, dosage_form, route "
        "ORDER BY normalized_name, dosage_form, route "
        "LIMIT :lim"
    )
    with get_engine().connect() as conn:
        rows = conn.execute(sa_text(sql), {"lim": int(limit)}).mappings().all()
    items: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        template = _DOC_TEMPLATES[i % len(_DOC_TEMPLATES)]
        items.append(
            {
                "question": template.format(
                    name=row["normalized_name"],
                    form=(row["dosage_form"] or "").lower(),
                    route=(row["route"] or "").lower(),
                ),
                "short_names": {s for s in row["short_names"] if s},
            }
        )
    return items


# ---------------------------------------------------------------------------
# Arm runners (I/O)
# ---------------------------------------------------------------------------


def _timed_embed(provider: Any, question: str, sink: list[float]) -> list[float]:
    # MUST route through embed_query, not provider.embed: instruction-tuned
    # providers (Qwen3) prefix queries and never instruct documents, and this is
    # the call prod's retriever makes. Using the document path here silently
    # penalizes the instructed arm and the A/B stops measuring what prod runs.
    from regwatch.process.embedder import embed_query

    t0 = time.perf_counter()
    vec = embed_query(provider, question)
    sink.append(time.perf_counter() - t0)
    return vec


def run_arm(
    name: str,
    provider: Any,
    search_fn: Any,
    gold: list[dict[str, Any]],
    refuse_questions: list[str],
    doc_items: list[dict[str, Any]],
    k: int,
) -> ArmResult:
    result = ArmResult(name=name)
    for item in gold:
        vec = _timed_embed(provider, item["question"], result.embed_latencies)
        t0 = time.perf_counter()
        hits = search_fn(vec, k=k)
        result.search_latencies.append(time.perf_counter() - t0)
        metas = [h.metadata for h in hits]
        result.ranks.append(rank_of_first_match(metas, item["expected_sources"]))
        result.top_scores.append(hits[0].score if hits else 0.0)
    for question in refuse_questions:
        vec = _timed_embed(provider, question, result.embed_latencies)
        t0 = time.perf_counter()
        hits = search_fn(vec, k=k)
        result.search_latencies.append(time.perf_counter() - t0)
        result.refuse_top_scores.append(hits[0].score if hits else 0.0)
    for item in doc_items:
        vec = _timed_embed(provider, item["question"], result.embed_latencies)
        t0 = time.perf_counter()
        hits = search_fn(vec, k=k)
        result.search_latencies.append(time.perf_counter() - t0)
        result.doc_ranks.append(doc_level_rank([h.metadata for h in hits], item["short_names"]))
    return result


def _preflight(profile_id: str) -> None:
    """Fail loudly on partial corpora -- a benchmark over missing vectors lies."""
    from sqlalchemy import text as sa_text

    from regwatch.store.db import get_engine
    from regwatch.store.embedding_profiles import profile_embedding_coverage

    with get_engine().connect() as conn:
        total_chunks = int(conn.execute(sa_text("SELECT count(*) FROM chunk")).scalar() or 0)
        null_legacy = int(
            conn.execute(sa_text("SELECT count(*) FROM chunk WHERE embedding IS NULL")).scalar()
            or 0
        )
    # Coverage is a ratio: 0/0 reports "complete". An empty corpus therefore
    # clears every downstream check while retrieving nothing.
    if total_chunks == 0:
        raise SystemExit("chunk table is empty: nothing to benchmark")
    if null_legacy:
        raise SystemExit(
            f"legacy arm incomplete: {null_legacy} chunk(s) missing legacy embeddings; "
            "re-run `regwatch rechunk` first"
        )
    coverage = profile_embedding_coverage(profile_id)
    if not coverage.complete:
        raise SystemExit(
            f"profile {profile_id} incomplete: {coverage.pending_chunks} pending chunk(s); "
            "re-run `regwatch rechunk` (or embedding-profile-backfill) first"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@app.command()
def run(
    profile: str = typer.Option(..., "--profile", help="Embedding profile id to benchmark."),
    k: int = typer.Option(8, "--k", min=1, help="Retrieval depth (prod uses 8)."),
    gold: Path = typer.Option(
        Path(__file__).parent / "gold_set.jsonl", "--gold", help="Gold set JSONL."
    ),
    doc_limit: int = typer.Option(
        150, "--doc-limit", min=0, help="Auto doc-level questions (0 disables)."
    ),
    out: Path | None = typer.Option(None, "--out", help="Write full JSON report here."),
) -> None:
    from config.settings import get_settings

    from regwatch.process.embedder import (
        get_embedding_provider,
        get_embedding_provider_for_profile,
    )
    from regwatch.store.db import init_db
    from regwatch.store.embedding_profiles import (
        get_embedding_profile,
        similarity_search_profile,
    )
    from regwatch.store.vector_store import similarity_search

    console = Console()
    init_db()
    _preflight(profile)

    gold_items, refuse_questions = load_gold_items(gold)
    doc_items = build_doc_items(doc_limit) if doc_limit else []
    console.print(
        f"[cyan]benchmark: {len(gold_items)} gold + {len(refuse_questions)} must-refuse "
        f"+ {len(doc_items)} doc-level questions, k={k}[/cyan]"
    )

    profile_row = get_embedding_profile(profile)
    legacy = run_arm(
        "legacy",
        get_embedding_provider("openai"),
        similarity_search,
        gold_items,
        refuse_questions,
        doc_items,
        k,
    )
    prof = run_arm(
        profile,
        get_embedding_provider_for_profile(profile_row),
        lambda vec, k: similarity_search_profile(profile, vec, k=k),
        gold_items,
        refuse_questions,
        doc_items,
        k,
    )

    legacy_summary = legacy.summary()
    prof_summary = prof.summary()
    table = Table(title=f"Retrieval A/B: legacy vs {profile} (k={k})")
    table.add_column("metric", style="bold")
    table.add_column("legacy", justify="right")
    table.add_column("profile", justify="right")
    for key in (
        "gold_hit_rate",
        "gold_mrr",
        "doc_hit_rate",
        "doc_mrr",
        "answerable_top_score_mean",
        "answerable_top_score_min",
        "refuse_top_score_mean",
        "refuse_top_score_max",
        "embed_p50_ms",
        "embed_p95_ms",
        "search_p50_ms",
        "search_p95_ms",
    ):
        table.add_row(key, f"{legacy_summary[key]:.3f}", f"{prof_summary[key]:.3f}")
    console.print(table)

    threshold = get_settings().refusal_score_threshold
    console.print(
        f"REFUSAL_SCORE_THRESHOLD={threshold}: an answerable min top-score at or "
        "below it would start refusing real questions; a refuse max above it "
        "would start answering ones it should not. Unfiltered scores -- the "
        "production path scores a filtered, reranked set."
    )

    comparison = compare_arms(legacy_summary, prof_summary)
    if comparison.degenerate:
        console.print(f"[red]diagnostic did not measure anything: {comparison.degenerate_reason}")
    elif comparison.observations:
        console.print("[yellow]observations (embedding geometry only, not a verdict):[/yellow]")
        for note in comparison.observations:
            console.print(f"[yellow]  - {note}[/yellow]")
    else:
        console.print("[dim]no metric moved by more than one item of noise[/dim]")
    console.print(
        "[dim]This is a diagnostic. Promotion is decided by "
        "`run_eval --profile <arm>`, which runs the production pipeline.[/dim]"
    )

    if out:
        out.write_text(
            json.dumps(
                {
                    "kind": "embedding-diagnostic",
                    "not_a_release_gate": (
                        "unfiltered vector search; no product/form filters, no "
                        "current-version scoping, no rerank. Promotion is decided "
                        "by run_eval --profile."
                    ),
                    "k": k,
                    "refusal_score_threshold": threshold,
                    "legacy": legacy_summary,
                    "profile": prof_summary,
                    "observations": comparison.observations,
                    "degenerate": comparison.degenerate,
                    "degenerate_reason": comparison.degenerate_reason,
                    "gold_ranks": {"legacy": legacy.ranks, "profile": prof.ranks},
                    "doc_ranks": {"legacy": legacy.doc_ranks, "profile": prof.doc_ranks},
                },
                indent=2,
            )
        )
        console.print(f"report written to {out}")
    # Operational status only: did the diagnostic run? Never a quality verdict.
    raise typer.Exit(code=2 if comparison.degenerate else 0)


if __name__ == "__main__":
    app()
