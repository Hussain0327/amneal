"""Eval runner.

Reads `gold_set.jsonl`, runs each question through `grounded_qa.ask`, and
prints a scorecard. With `--check-thresholds`, exits non-zero if any metric
is below the spec §12 targets.

Targets (POC):
  recall@8           ≥ 0.90
  citation_precision ≥ 0.95
  refusal_accuracy   ≥ 0.95

Quality is not the only dimension: end-to-end p50/p95 per question are reported
on every run, and p95 is gated against LATENCY_P95_CEILING_MS (exit 5), so a
change that buys recall with latency cannot pass unseen.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from config.settings import get_settings
from rich.console import Console
from rich.table import Table

from regwatch.eval import prod_mode, run_fingerprint
from regwatch.eval.metrics import GoldItem, Scorecard, evaluate
from regwatch.generate.grounded_qa import ask
from regwatch.generate.prompts import generation_prompt_manifest
from regwatch.store.db import init_db
from regwatch.store.vector_store import collection_size

app = typer.Typer(
    no_args_is_help=False, add_completion=False, help="Evaluate REGWATCH on a gold set."
)


# BLOCKING floors. --check-thresholds exits 2 when a metric here misses.
#
# Ratcheted to the first real measurement (eval_run id=1, 2026-08-05, arm
# ep_2e7368b354d911ea3a013c3125e276c2, 66 chunks / 8 docs): recall_at_k 0.814,
# citation_precision 0.756. Each floor sits slightly BELOW its measurement on
# purpose -- the eval drives a live LLM synthesizer, so the numbers drift
# run to run, and a floor set at the measurement would flake red on noise
# rather than on a regression.
#
# What this gate now means: "no worse than the day it was first measured." It
# is a ratchet, not a quality bar. Raise these as quality improves; never
# lower one without recording why.
#
# refusal_accuracy is BLOCKING again as of 2026-08-06 (issue #161). Its earlier
# 0.710 was not a defect measurement: it scored a must_refuse row on whether the
# reply wore the "refused" status, and the 12 seeded-product rows come back as
# "clarify" with ZERO citations and no claim about the question -- a withheld
# answer, which is exactly what the label asserts. Adjudicated by re-scoring the
# two recorded scorecard artifacts row by row (2026-08-05 and 2026-08-06 CI
# runs): under the withhold policy every refusal row is correct in both, 16/16
# and 15/15, and NO gold row needed relabelling. See metrics.withheld_answer and
# docs/EVAL_STATUS.md.
#
# refusal_accuracy is UN-GATED again as of 2026-08-06, by owner decision, and
# this time not because its labels are disputed: the product is moving to a
# conversational Ask layer that is not meant to refuse. Gating a system on how
# often it declines, while deliberately teaching it to stop declining, would
# fail the build for doing the new thing correctly. The metric and its 16 gold
# rows are slated for removal from the codebase once that direction lands; it
# stays measured, printed and persisted until then so the transition is visible
# rather than silent.
#
# The adjudication that made it briefly blockable still stands and is still
# enforced by metrics.withheld_answer and its tests -- what changed is whether
# the number should stop a build, not what it means. See docs/EVAL_STATUS.md.
#
# citation_precision LOWERED 0.74 -> 0.70 on 2026-08-11, by owner decision, and
# recorded here because this file's own rule is that a floor is never lowered
# without saying why.
#
# What was measured, all three arms on the same 62-row gold set and the same
# Databricks/Qwen corpus, 0 errored turns:
#
#     arm   recall_at_k   citation_precision
#     v5      0.8372          0.7694
#     v6      0.8372          0.7619
#     v7      0.8372          0.7341
#
# recall_at_k is IDENTICAL across the three, so retrieval is untouched and the
# spread is entirely the answer path. The v7 drop is not v7 citing worse: on
# rows that produced an answer its precision is fine. It is v7 failing to
# produce a citable answer at all on 4 rows (material_drop, no_valid_citations
# x2, malformed_structure) where the v5 claims-JSON path had zero such
# failures. Those rows contribute 0 while staying in the denominator, which is
# the documented and deliberate behaviour in metrics.evaluate.
#
# So 0.70 is a floor for the PROSE ERA, not a verdict that 0.7341 is good. It
# sits ~3.4pp below the measured v7 arm, the same "slightly below the
# measurement" margin the other floors use to absorb live-LLM run-to-run drift.
# The 4 failing rows are a real defect and are tracked separately; this floor
# is deliberately NOT set where it would pass only if they were fixed, because
# a gate that cannot go green blocks every unrelated change too.
THRESHOLDS = {
    "recall_at_k": 0.80,
    "citation_precision": 0.70,
}

# BLOCKING ceiling on END-TO-END p95 latency per gold question, milliseconds.
# --check-thresholds exits EXIT_LATENCY_REGRESSION when a run sits above it.
# Added because quality alone is not a verdict: a retrieval change that lifts
# recall by 0.01 and doubles p95 is a regression the old gate could not see.
#
# PROVISIONAL, and unlike the floors above it is NOT a ratchet against a
# measurement -- no eval run has ever recorded a latency, so there is nothing
# to ratchet to yet. It is derived from the transport's own worst case instead:
# llm_timeout_s (60s) x (1 attempt + llm_max_retries 2) = 180s is the longest a
# synthesis chain can run before it gives up, so a p95 above that means several
# turns each burned the full retry budget and STILL returned. That is an outage
# or a regression, not noise. REPLACE this with "observed p95 + margin" once CI
# has recorded a few runs, and record the measurement here the way the floors
# above record theirs.
#
# Why it is this loose on purpose: the CI eval drives shared Databricks
# endpoints, and concurrent PR evals collide on QPS -- the documented 2026-08-06
# incident that MAX_UNMEASURED_FRACTION below exists for. A tight end-to-end
# gate would go red on other people's traffic, and a gate that flakes is a gate
# someone deletes. p50/p95 are REPORTED on every run, so the trend is visible
# long before this ceiling has teeth.
#
# Retrieval-phase p95 would be the stable dimension to gate (it excludes the
# synthesizer, which is where the QPS variance lives), but grounded_qa.ask()
# returns one opaque QAResult with no phase timings, so the eval cannot split
# the phases without changing that contract. End-to-end is what is measurable
# today, which is why the ceiling is generous rather than tight.
LATENCY_P95_CEILING_MS = 180_000.0

# Env override for the ceiling, so a deliberately slower arm can be evaluated
# without editing this file. Validated, never silently defaulted -- see
# _latency_ceiling_ms.
LATENCY_P95_CEILING_ENV = "REGWATCH_EVAL_LATENCY_P95_CEILING_MS"

# ASPIRATIONAL targets. Reported beside each value, never blocking.
#
# These are the original 0.90/0.95/0.95 figures. They were written against echo
# (hash-based) embeddings with REFUSAL_SCORE_THRESHOLD=0.0 and have never been
# demonstrated reachable on real geometry against this corpus. They are recorded
# here as where the system SHOULD get to, not as evidence that it can.
TARGETS = {
    "recall_at_k": 0.90,
    "citation_precision": 0.95,
    "refusal_accuracy": 0.95,
}

# Above this share of transport-failed turns, --check-thresholds exits 3 instead
# of scoring: the run did not measure the system. 10% of the 62-row gold set is
# ~6 turns. The 2026-08-06 rate-limit run lost 5 (8%) and is the case this is
# calibrated to let through WITH its metrics intact, since excluding those five
# returns every metric to its recorded baseline. A worse outage must not be able
# to pass the gate on a shrunken denominator.
MAX_UNMEASURED_FRACTION = 0.10

# --assert-prod-mode failed: this run is not measuring what production serves,
# so its scorecard cannot speak for production. Distinct from 2 (a metric
# missed) and 3 (the run could not measure) because the fix is different: 2 is a
# regression, 3 is an outage, 4 is a misconfigured run.
EXIT_WRONG_ARM = 4

# The run's end-to-end p95 sat above the latency ceiling while every quality
# floor cleared. Its own code rather than 2 for two reasons: the eval_run
# ledger's `passed` column is computed from THRESHOLDS alone
# (ledger.scorecard_passed), so folding a latency miss into 2 would print a red
# gate beside a stored row that says passed=true; and it sends the reader
# somewhere else -- 2 is retrieval or the prompt, 5 is the provider and the
# retry budget.
EXIT_LATENCY_REGRESSION = 5


def _latency_ceiling_ms() -> float:
    """The p95 ceiling this run gates on, environment override included.

    A plain environment variable rather than a config.settings field: this is a
    property of the GATE, like THRESHOLDS and MAX_UNMEASURED_FRACTION, not of
    the system under test.

    An unusable override RAISES rather than falling back to the default. The
    operator asked for a specific ceiling; quietly gating (and printing)
    against a different one is worse than stopping. Called at the top of run(),
    before the corpus, the DB and the first provider call, so a typo costs no
    LLM spend.

    Returns:
        The ceiling in milliseconds.

    Raises:
        SystemExit: The override is set but is not a positive, finite number.
    """
    raw = (os.environ.get(LATENCY_P95_CEILING_ENV) or "").strip()
    if not raw:
        return LATENCY_P95_CEILING_MS
    try:
        ceiling = float(raw)
    except ValueError as exc:
        raise SystemExit(
            f"{LATENCY_P95_CEILING_ENV}={raw!r} is not a number of milliseconds"
        ) from exc
    if not math.isfinite(ceiling) or ceiling <= 0:
        # inf would disable the gate while looking like it was configured, and
        # a nan comparison is False, which is the same silent pass.
        raise SystemExit(
            f"{LATENCY_P95_CEILING_ENV}={raw!r} must be a positive, finite number "
            "of milliseconds"
        )
    return ceiling


def _latency_exceeds(sc: Scorecard, ceiling_ms: float) -> bool:
    """Whether this run's end-to-end p95 sat above the ceiling.

    False when nothing was timed: a run with no measured turn has no latency to
    judge, and MAX_UNMEASURED_FRACTION already fails a run that could not
    measure. The report and the exit code share this predicate so the printed
    verdict can never disagree with the build's.
    """
    return sc.latency_p95_ms is not None and sc.latency_p95_ms > ceiling_ms


def _latency_summary(sc: Scorecard, ceiling_ms: float) -> str:
    """The reported latency line, as a string so it is assertable in tests."""
    if sc.latency_p95_ms is None or sc.latency_p50_ms is None:
        return "[yellow]latency: no turn was timed, so the p95 gate did not run[/yellow]"
    verdict = "[red]FAIL[/red]" if _latency_exceeds(sc, ceiling_ms) else "[green]ok[/green]"
    # Parentheses, not brackets: everything here goes through rich's markup
    # parser, and a literal "[" is one grammar change away from being read as a
    # tag.
    return (
        f"latency p50={sc.latency_p50_ms:.0f}ms  p95={sc.latency_p95_ms:.0f}ms  "
        f"over {sc.latency_samples} measured turn(s)  "
        f"(end-to-end p95 gate <= {ceiling_ms:.0f}ms: {verdict})"
    )


def _assert_prod_mode() -> None:
    """Refuse to score an arm production does not serve.

    Read BEFORE the corpus, the DB and the first provider call, so a
    misconfigured run costs nothing and cannot occupy the serialized live-eval
    slot. See regwatch.eval.prod_mode for why this exists.
    """
    try:
        expected = prod_mode.load_manifest()
        settings = get_settings()
        effective = {key: getattr(settings, key) for key in expected if hasattr(settings, key)}
        found = prod_mode.mismatches(effective, expected)
    except prod_mode.ManifestError as exc:
        Console().print(f"[red]production-mode contract unusable: {exc}[/red]")
        sys.exit(EXIT_WRONG_ARM)
    if found:
        console = Console()
        console.print(
            "[red]This run does not measure what production serves, so its "
            "scorecard cannot speak for production:[/red]"
        )
        for line in found:
            console.print(f"  [red]{line}[/red]")
        console.print(
            "Fix the run's env (or config/prod_mode.json, if production moved) " "and re-run."
        )
        sys.exit(EXIT_WRONG_ARM)


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
                    category=str(row.get("category") or ""),
                    must_refuse=bool(row.get("must_refuse", False)),
                    must_clarify=bool(row.get("must_clarify", False)),
                    forbidden=row.get("forbidden") or [],
                )
            )
    return items


def _print_scorecard(sc: Scorecard, ceiling_ms: float) -> None:
    console = Console()
    table = Table(title="REGWATCH eval scorecard")
    table.add_column("metric", style="bold")
    table.add_column("value", justify="right")
    # "gate" and "target" are separate columns so the table can never imply the
    # aspirational number is the one being enforced. A single "threshold" column
    # is what let 0.90/0.95/0.95 read as validated acceptance criteria for as
    # long as they did.
    table.add_column("gate", justify="right")
    table.add_column("target", justify="right")
    table.add_column("status")
    for key in (
        "recall_at_k",
        # No threshold: MRR is reported to make rank movement visible, not to
        # gate on it. Gating a rank metric before the gold set is large enough
        # to make it stable would block releases on noise.
        "mrr",
        "citation_precision",
        "faithfulness",
        # No threshold: the pre-PR8 text rule, kept for trend continuity. See
        # eval/metrics.sentence_citation_rate.
        "sentence_citation_rate",
        "fact_recall",
        "refusal_accuracy",
    ):
        v = getattr(sc, key)
        thr = THRESHOLDS.get(key)
        target = TARGETS.get(key)
        if thr is not None:
            status = "[green]ok[/green]" if v >= thr else "[red]FAIL[/red]"
        elif target is not None:
            # Measured and recorded, but nothing fails on it.
            status = "[yellow]not gated[/yellow]"
        else:
            status = "—"
        table.add_row(
            key,
            f"{v:.3f}",
            f"{thr:.2f}" if thr is not None else "—",
            f"{target:.2f}" if target is not None else "—",
            status,
        )
    console.print(table)
    # Outside the table on purpose: its "gate" column is a FLOOR, and a ceiling
    # printed in that column would read as "must be at least 180000ms".
    console.print(_latency_summary(sc, ceiling_ms))
    console.print(
        f"refused_correctly={sc.refused_correctly}  "
        f"clarified_correctly={sc.clarified_correctly}  "
        f"refused_incorrectly={sc.refused_incorrectly}  "
        f"cited_ungrounded={sc.cited_ungrounded}"
    )
    if sc.by_category:
        # The aggregate says quality moved; this says where. A re-chunk that
        # regresses only table questions is invisible in the headline number
        # until the table category is large enough to drag it, by which point
        # the cause is much harder to attribute.
        breakdown = Table(title="by category")
        breakdown.add_column("category", style="bold")
        breakdown.add_column("n", justify="right")
        breakdown.add_column("recall", justify="right")
        breakdown.add_column("mrr", justify="right")
        breakdown.add_column("cite prec", justify="right")
        breakdown.add_column("decision", justify="right")
        for cat in sorted(sc.by_category):
            row = sc.by_category[cat]

            def _fmt(key: str, r: dict[str, float] = row) -> str:
                return f"{r[key]:.3f}" if key in r else "—"

            breakdown.add_row(
                cat,
                f"{int(row['n'])}",
                _fmt("recall_at_k"),
                _fmt("mrr"),
                _fmt("citation_precision"),
                _fmt("decision_accuracy"),
            )
        console.print(breakdown)
    if sc.skipped:
        # Loud, explicit — never a silent pass. These items asserted multi-form
        # clarify behavior on a product the seeded corpus does not contain.
        absent = [d["q"] for d in sc.details if d.get("skipped")]
        console.print(
            f"[yellow]skipped {sc.skipped} must_clarify item(s) absent from the "
            f"seeded corpus (excluded from refusal_accuracy; still hard-gated "
            f"offline in the deterministic eval gate): {absent}[/yellow]"
        )
    if sc.errored:
        # Also loud: these rows left EVERY denominator because their transport
        # failed, so all metrics above are measured over fewer items than the
        # gold set has. A rising count means the provider is failing, not that
        # the system improved.
        broke = [(d.get("errored"), d["q"]) for d in sc.details if d.get("errored")]
        console.print(
            f"[yellow]{sc.errored}/{sc.n} turn(s) failed in transport and are excluded "
            f"from every metric (they measured nothing): {broke}[/yellow]"
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


def _verify_gold(items: list[GoldItem]) -> None:
    """Refuse to score a gold set that disagrees with the corpus.

    A scorecard produced against mis-pinned expectations is not evidence, it is
    noise wearing a number. Verifying BEFORE the run also means the failure costs
    no LLM calls. See eval/verify_gold.py for the 25%-defect-rate incident that
    motivated this.
    """
    from regwatch.eval.verify_gold import verify_items

    defects = verify_items(items)
    if not defects:
        return
    console = Console()
    console.print(f"[red]gold set does not match the corpus ({len(defects)} defect(s)):[/red]")
    for defect in defects:
        console.print(f"  [red]-[/red] {defect}")
    raise SystemExit(2)


def _persist(
    fingerprint: run_fingerprint.RunFingerprint,
    sc: Scorecard,
    gold: Path,
    artifact: dict[str, object],
) -> None:
    """Record the run, reporting the outcome either way. Never raises.

    The measurement is the product; the ledger is bookkeeping on top of it. A
    DB hiccup must not turn a green gate red -- nor abort before the threshold
    check and turn a red gate into no gate at all. A failed write is printed,
    never swallowed silently.
    """
    from regwatch.eval.ledger import record_eval_run

    console = Console()
    try:
        # Round-trip through JSON so a non-serializable provenance value
        # (a Path, an enum) degrades to its string form here rather than
        # failing the INSERT with the scorecard already on screen.
        payload = json.loads(json.dumps(artifact, default=str))
        run_id = record_eval_run(
            fingerprint=fingerprint,
            scorecard=sc,
            thresholds=THRESHOLDS,
            gold_path=gold,
            artifact=payload,
        )
    except Exception as exc:
        console.print(f"[yellow]eval_run ledger write failed ({exc}); scorecard stands[/yellow]")
        return
    console.print(f"[dim]recorded eval_run id={run_id}[/dim]")


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
    persist: bool = typer.Option(
        True,
        "--persist/--no-persist",
        help=(
            "Record this run in the eval_run ledger so scorecards are comparable "
            "across runs. --no-persist for a throwaway local run."
        ),
    ),
    profile: str = typer.Option(
        run_fingerprint.LEGACY,
        "--profile",
        help=(
            "Embedding arm for this run: 'legacy' (chunk.embedding column) or a "
            "registered profile id. Applies to this process only -- run each arm "
            "in its own invocation so the two are independent."
        ),
    ),
    # Annotated form, unlike the options above it, because this one must default
    # to False for a caller that invokes run() as a PLAIN FUNCTION -- which the
    # tests do. In the older `x: bool = typer.Option(False, ...)` form the
    # runtime default is an OptionInfo object, and OptionInfo is TRUTHY, so a
    # direct caller that simply omitted this argument would silently switch the
    # production-mode assertion ON. Here the declared default is a real False.
    assert_prod_mode: Annotated[
        bool,
        typer.Option(
            "--assert-prod-mode",
            help=(
                "Refuse to run unless this process's answer-path settings match "
                "config/prod_mode.json. The blocking CI eval passes this so a "
                "green check cannot mean 'some arm passed'."
            ),
        ),
    ] = False,
) -> None:
    # First, and unconditionally: it is what the report PRINTS as the ceiling
    # as well as what --check-thresholds enforces, so a run that cannot resolve
    # it would misinform even without the gate.
    latency_ceiling_ms = _latency_ceiling_ms()
    if assert_prod_mode:
        _assert_prod_mode()
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
    _verify_gold(items)
    # Built before the run so the artifact describes the configuration the
    # questions actually executed under, not one a later flip could rewrite.
    fingerprint = run_fingerprint.build(profile, THRESHOLDS)
    sc = evaluate(items, ask_callable=ask)
    _print_scorecard(sc, latency_ceiling_ms)
    _print_fingerprint(fingerprint)
    # Both halves of the provenance story, neither dropped: the fingerprint
    # says which corpus/arm/config produced the run, the prompt manifest says
    # which prompts did. Built unconditionally now because the ledger stores it
    # too, so --out and --persist can never disagree about what this run was.
    artifact = {
        # 3: the scorecard grew latency_p50_ms/latency_p95_ms/latency_samples
        # and the artifact grew the ceiling they were judged against.
        "artifact_schema_version": 3,
        "fingerprint": fingerprint.to_dict(),
        "prompts": generation_prompt_manifest(),
        "scorecard": asdict(sc),
        # Recorded because the ceiling is env-overridable: without it a stored
        # p95 cannot be read as a pass or a fail after the fact.
        "latency_p95_ceiling_ms": latency_ceiling_ms,
    }
    if out:
        # default=str keeps non-JSON fingerprint values (paths, enums) from
        # failing the write.
        out.write_text(json.dumps(artifact, indent=2, default=str))

    if persist:
        _persist(fingerprint, sc, gold, artifact)

    # AFTER the ledger write on purpose: a run that fails the gate is a real
    # measurement and is exactly the row a later investigation needs. Exiting
    # first would record only the passing runs.
    if check_thresholds:
        # "Could not measure" and "measured badly" are different build failures,
        # and reporting the first as the second is what sent two PRs red on
        # 2026-08-06. Transport-failed turns leave the denominators (see
        # metrics.unmeasured_turn), so without this a provider outage could
        # shrink the gate to a handful of lucky rows and pass. Checked BEFORE
        # the thresholds so the message names the real problem.
        if sc.n and sc.errored / sc.n > MAX_UNMEASURED_FRACTION:
            console = Console()
            console.print(
                f"[red]{sc.errored}/{sc.n} turns failed in transport "
                f"({sc.errored / sc.n:.0%} > {MAX_UNMEASURED_FRACTION:.0%} allowed). "
                "This run did not measure the system -- the metrics above are "
                "computed over too few rows to mean anything. Fix the provider, "
                "then re-run; do NOT read this as a quality regression.[/red]"
            )
            sys.exit(3)
        violations = [
            (k, getattr(sc, k), thr) for k, thr in THRESHOLDS.items() if getattr(sc, k) < thr
        ]
        if violations:
            sys.exit(2)
        # Checked LAST, so a run that regressed on both reports the quality
        # regression: that is the more important finding, and exit 2 is the
        # code CI and every runbook already know.
        if _latency_exceeds(sc, latency_ceiling_ms):
            console = Console()
            console.print(
                f"[red]end-to-end p95 {sc.latency_p95_ms:.0f}ms is above the "
                f"{latency_ceiling_ms:.0f}ms ceiling, over {sc.latency_samples} "
                "measured turn(s). Every quality floor cleared, so this is a "
                "latency regression: look at the provider and the retry budget "
                f"before the prompt. Override with {LATENCY_P95_CEILING_ENV} only "
                "with a recorded reason.[/red]"
            )
            sys.exit(EXIT_LATENCY_REGRESSION)


if __name__ == "__main__":
    app()
