"""The eval gate's latency dimension.

Until this existed the gate scored quality only, so a retrieval or prompt
change that lifted recall by 0.01 and doubled p95 passed as an improvement.
These tests pin the three properties that make the new dimension trustworthy:

  * the percentile is nearest-rank, so every reported number is a latency some
    turn actually took (and an empty sample reports NOTHING rather than a 0.0
    that would clear the ceiling);
  * the harness times the ask() call and nothing else, and a transport-failed
    turn keeps its per-row number but leaves the summary, exactly as it leaves
    every other denominator;
  * the ceiling gates -- with its own exit code, below the quality floors and
    below the could-not-measure guard in priority.

No network, no DB, no model: the clock is a scripted fake and the scorecard is
handed to the CLI directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from regwatch.eval import metrics as metrics_mod
from regwatch.eval import run_eval, run_fingerprint
from regwatch.eval.metrics import GoldItem, Scorecard, evaluate, percentile

# --- the shared percentile ---------------------------------------------------


def test_percentile_is_nearest_rank_over_an_odd_sample() -> None:
    values = [30.0, 10.0, 20.0]  # unsorted on purpose: the callers do not sort
    assert percentile(values, 50) == 20.0
    assert percentile(values, 95) == 30.0
    assert percentile(values, 0) == 10.0


def test_percentile_over_an_even_sample_never_averages_the_middle_pair() -> None:
    """p50 of four samples is the 2nd value, not the 2.5th.

    An interpolated percentile names a latency no turn experienced, which is
    the opposite of what a reader takes a p95 to mean.
    """
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0


def test_percentile_of_a_single_sample_is_that_sample() -> None:
    assert percentile([7.5], 50) == 7.5
    assert percentile([7.5], 95) == 7.5


def test_percentile_of_nothing_is_none_rather_than_zero() -> None:
    """0.0 would read as an instantaneous turn and CLEAR the latency ceiling.

    "Nothing was measured" and "everything was instant" must never look alike
    to a gate.
    """
    assert percentile([], 50) is None
    assert percentile([], 95) is None


def test_percentile_reports_a_value_some_sample_took() -> None:
    """The contract route_shadow_report depends on after dropping its copy."""
    values = [float(ms) for ms in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0


# --- timing the eval loop ----------------------------------------------------


@dataclass
class _Result:
    """The subset of QAResult that eval.metrics reads."""

    answer: str = "Two-way crossover [PSG_1, p.4]."
    citations: list[Any] = field(default_factory=list)
    refused: bool = False
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    status: str = "answer"
    reason: str | None = None


def _transport_error() -> _Result:
    """A turn whose provider failed: it measured nothing (metrics.unmeasured_turn)."""
    return _Result(answer="", refused=True, status="error", reason="provider_error")


def _clock(*elapsed_ms: float) -> Callable[[], float]:
    """A perf_counter stand-in scripted with one duration per gold item.

    ``evaluate`` reads the clock exactly twice per item, so each element yields
    one (start, stop) pair. A full second of dead time is inserted BETWEEN
    pairs: it must never appear in any reported latency, which is what proves
    the harness times the ask() call rather than the loop.

    Args:
        *elapsed_ms: How long each item's ask() call should appear to take.

    Returns:
        A zero-argument callable returning the next monotonic stamp, in
        seconds. It raises StopIteration if called more often than scripted,
        which is the signal that the loop changed how it times an item.
    """
    stamps: list[float] = []
    now = 0.0
    for ms in elapsed_ms:
        stamps.append(now)
        now += ms / 1000.0
        stamps.append(now)
        now += 1.0
    cursor = iter(stamps)
    return lambda: next(cursor)


def _gold(n: int) -> list[GoldItem]:
    """``n`` answerable items with nothing to find, so only timing varies."""
    return [GoldItem(question=f"q{i}?", expected_sources=[]) for i in range(n)]


def test_evaluate_times_each_item_and_summarizes_the_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metrics_mod, "perf_counter", _clock(100.0, 200.0, 300.0))

    sc = evaluate(_gold(3), ask_callable=lambda _q: _Result())

    assert [d["latency_ms"] for d in sc.details] == [100.0, 200.0, 300.0]
    assert sc.latency_samples == 3
    assert sc.latency_p50_ms == 200.0
    assert sc.latency_p95_ms == 300.0


def test_a_transport_failure_keeps_its_row_but_leaves_the_percentiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 that hung for 90s describes the provider, not the system.

    Same rule the other denominators already follow (metrics.unmeasured_turn):
    the row stays visible with its own number, and the summary excludes it.
    """
    monkeypatch.setattr(metrics_mod, "perf_counter", _clock(100.0, 90_000.0, 300.0))
    replies = [_Result(), _transport_error(), _Result()]

    sc = evaluate(_gold(3), ask_callable=lambda _q: replies.pop(0))

    assert [d["latency_ms"] for d in sc.details] == [100.0, 90_000.0, 300.0]
    assert sc.errored == 1
    assert sc.latency_samples == 2
    assert sc.latency_p50_ms == 100.0
    assert sc.latency_p95_ms == 300.0


def test_an_empty_gold_set_reports_no_latency_at_all() -> None:
    sc = evaluate([], ask_callable=lambda _q: _Result())

    assert sc.latency_samples == 0
    assert sc.latency_p50_ms is None
    assert sc.latency_p95_ms is None


# --- the gate ----------------------------------------------------------------


def _sc(**over: Any) -> Scorecard:
    """A scorecard clearing every quality floor, with fast turns; overridable.

    The quality numbers are the recorded 2026-08-05 baseline the ratchet tests
    use, so a case here can only ever fail on the dimension it changes.
    """
    base: dict[str, Any] = {
        "n": 62,
        "recall_at_k": 0.814,
        "mrr": 0.506,
        "citation_precision": 0.756,
        "faithfulness": 0.826,
        "fact_recall": 0.622,
        "refusal_accuracy": 0.903,
        "latency_p50_ms": 4_000.0,
        "latency_p95_ms": 9_000.0,
        "latency_samples": 62,
    }
    base.update(over)
    return Scorecard(**base)


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    scorecard: Scorecard,
    out: Path | None = None,
    check_thresholds: bool = True,
) -> int:
    """Drive the real CLI with the corpus, the model and the ledger stubbed out.

    Everything the latency gate itself touches is the real code path: ceiling
    resolution, the printed report, artifact assembly, the exit code. The
    fingerprint is stubbed because its corpus digest is a live DB query and
    these cases must not need one.

    Returns:
        The process exit code the CLI would have produced (0 when it returned).
    """
    gold = tmp_path / "gold.jsonl"
    gold.write_text('{"question": "q?", "expected_sources": []}\n', encoding="utf-8")
    monkeypatch.setattr(run_eval, "init_db", lambda: None)
    monkeypatch.setattr(run_eval, "collection_size", lambda: 5)
    monkeypatch.setattr(run_eval, "evaluate", lambda *_a, **_k: scorecard)
    monkeypatch.setattr(run_eval, "_verify_gold", lambda _items: None)
    monkeypatch.setattr(
        run_fingerprint, "build", lambda *_a, **_k: run_fingerprint.RunFingerprint()
    )
    try:
        run_eval.run(
            gold=gold,
            check_thresholds=check_thresholds,
            out=out,
            persist=False,
            profile="legacy",
        )
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_a_fast_run_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _run_cli(monkeypatch, tmp_path, scorecard=_sc()) == 0


def test_a_p95_above_the_ceiling_fails_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The case the dimension exists for: quality held, latency did not."""
    slow = _sc(latency_p95_ms=run_eval.LATENCY_P95_CEILING_MS + 1)

    code = _run_cli(monkeypatch, tmp_path, scorecard=slow)

    assert code == run_eval.EXIT_LATENCY_REGRESSION
    assert code not in (0, 2, 3, 4), "a latency regression must be its own verdict"


def test_a_p95_exactly_at_the_ceiling_still_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ceiling is inclusive; a gate that fails AT its own limit is a trap."""
    at_limit = _sc(latency_p95_ms=run_eval.LATENCY_P95_CEILING_MS)

    assert _run_cli(monkeypatch, tmp_path, scorecard=at_limit) == 0


def test_a_run_with_no_timed_turn_does_not_trip_the_latency_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No sample is not a slow run. The unmeasured guard owns that failure."""
    untimed = _sc(latency_p50_ms=None, latency_p95_ms=None, latency_samples=0)

    assert _run_cli(monkeypatch, tmp_path, scorecard=untimed) == 0


def test_latency_is_only_reported_without_check_thresholds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An observability run reports the number; only the gate enforces it."""
    slow = _sc(latency_p95_ms=run_eval.LATENCY_P95_CEILING_MS * 10)

    code = _run_cli(monkeypatch, tmp_path, scorecard=slow, check_thresholds=False)

    assert code == 0


@pytest.mark.parametrize(
    ("field_name", "value"), [("recall_at_k", 0.79), ("citation_precision", 0.69)]
)
def test_the_quality_floors_still_gate_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field_name: str, value: float
) -> None:
    """The latency dimension must not have moved the floors it sits beside."""
    code = _run_cli(monkeypatch, tmp_path, scorecard=_sc(**{field_name: value}))

    assert code == 2, f"{field_name}={value} is below its floor and must still exit 2"


def test_a_quality_regression_outranks_a_latency_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both wrong reports the quality miss: it is the more important finding,
    and exit 2 is the code every runbook already knows."""
    both = _sc(recall_at_k=0.0, citation_precision=0.0, latency_p95_ms=10**9)

    assert _run_cli(monkeypatch, tmp_path, scorecard=both) == 2


def test_a_run_that_could_not_measure_outranks_the_latency_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider outage inflates latency; naming it a latency regression would
    send someone tuning retrieval instead of fixing the provider."""
    outage = _sc(errored=20, latency_p95_ms=10**9)

    assert _run_cli(monkeypatch, tmp_path, scorecard=outage) == 3


def test_the_report_json_carries_the_latency_numbers_and_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stored p95 is unreadable later without the ceiling it was judged
    against, because that ceiling is env-overridable."""
    out = tmp_path / "artifact.json"

    assert _run_cli(monkeypatch, tmp_path, scorecard=_sc(), out=out) == 0

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["scorecard"]["latency_p50_ms"] == 4_000.0
    assert artifact["scorecard"]["latency_p95_ms"] == 9_000.0
    assert artifact["scorecard"]["latency_samples"] == 62
    assert artifact["latency_p95_ceiling_ms"] == run_eval.LATENCY_P95_CEILING_MS
    assert artifact["artifact_schema_version"] == 3


# --- the ceiling override ----------------------------------------------------


def test_the_env_override_tightens_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(run_eval.LATENCY_P95_CEILING_ENV, "1000")

    code = _run_cli(monkeypatch, tmp_path, scorecard=_sc(latency_p95_ms=9_000.0))

    assert code == run_eval.EXIT_LATENCY_REGRESSION


def test_the_env_override_loosens_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deliberately slower arm can be evaluated without editing the source."""
    monkeypatch.setenv(run_eval.LATENCY_P95_CEILING_ENV, "500000")

    code = _run_cli(monkeypatch, tmp_path, scorecard=_sc(latency_p95_ms=300_000.0))

    assert code == 0


def test_a_blank_override_falls_back_to_the_declared_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset variable and one exported empty are the same intent."""
    monkeypatch.setenv(run_eval.LATENCY_P95_CEILING_ENV, "   ")

    assert run_eval._latency_ceiling_ms() == run_eval.LATENCY_P95_CEILING_MS


@pytest.mark.parametrize("raw", ["soon", "inf", "nan", "0", "-1"])
def test_an_unusable_override_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Gating against a ceiling the operator did not ask for is worse than
    stopping, and 'inf'/'nan' would disable the gate while looking configured.
    """
    monkeypatch.setenv(run_eval.LATENCY_P95_CEILING_ENV, raw)

    with pytest.raises(SystemExit) as excinfo:
        run_eval._latency_ceiling_ms()

    assert run_eval.LATENCY_P95_CEILING_ENV in str(excinfo.value)


def test_an_unusable_override_stops_the_run_before_it_scores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolved before the corpus, the DB and the first provider call, so a
    typo in the variable costs no LLM spend: evaluate() must never run."""
    monkeypatch.setenv(run_eval.LATENCY_P95_CEILING_ENV, "soon")

    def _must_not_run(*_a: Any, **_k: Any) -> Scorecard:
        raise AssertionError("the run scored against an unusable ceiling")

    monkeypatch.setattr(run_eval, "evaluate", _must_not_run)
    monkeypatch.setattr(run_eval, "init_db", lambda: None)
    gold = tmp_path / "gold.jsonl"
    gold.write_text('{"question": "q?", "expected_sources": []}\n', encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        run_eval.run(
            gold=gold,
            check_thresholds=True,
            out=None,
            persist=False,
            profile="legacy",
        )

    assert run_eval.LATENCY_P95_CEILING_ENV in str(excinfo.value)


# --- the reported line -------------------------------------------------------


def test_the_reported_line_names_the_verdict() -> None:
    """The printed report and the exit code share one predicate, so a reader
    can never see 'ok' beside a red build."""
    fast = run_eval._latency_summary(_sc(), run_eval.LATENCY_P95_CEILING_MS)
    slow = run_eval._latency_summary(
        _sc(latency_p95_ms=run_eval.LATENCY_P95_CEILING_MS + 1),
        run_eval.LATENCY_P95_CEILING_MS,
    )
    untimed = run_eval._latency_summary(
        _sc(latency_p50_ms=None, latency_p95_ms=None, latency_samples=0),
        run_eval.LATENCY_P95_CEILING_MS,
    )

    assert "p50=4000ms" in fast
    assert "p95=9000ms" in fast
    assert "62 measured turn(s)" in fast
    assert "ok" in fast and "FAIL" not in fast
    assert "FAIL" in slow
    assert "no turn was timed" in untimed
