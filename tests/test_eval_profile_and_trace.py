"""Tests for the per-arm eval wiring: profile selection, trace, fingerprint.

These pin the properties that make two scorecards comparable at all -- that the
run really executed the arm it claims, and that the artifact says which corpus
and configuration produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from config.settings import get_settings

from regwatch.eval import run_eval, run_fingerprint
from regwatch.eval.metrics import GoldItem, evaluate


@pytest.fixture(autouse=True)
def _restore_active_profile() -> Any:
    """_apply_profile configures the process by writing os.environ directly, which
    is correct for a CLI and toxic in a test session: without this, one test here
    would leave ACTIVE_EMBEDDING_PROFILE set and every later test in the same
    process would silently retrieve through the wrong arm."""
    import os as _os

    before = _os.environ.get("ACTIVE_EMBEDDING_PROFILE")
    try:
        yield
    finally:
        if before is None:
            _os.environ.pop("ACTIVE_EMBEDDING_PROFILE", None)
        else:
            _os.environ["ACTIVE_EMBEDDING_PROFILE"] = before
        get_settings.cache_clear()


@dataclass
class _Citation:
    short_name: str
    page: int
    chunk_id: str
    doc_id: int
    version_id: int
    score: float
    snippet: str = "should not reach the artifact"


@dataclass
class _Result:
    answer: str = ""
    citations: list[Any] = None  # type: ignore[assignment]
    refused: bool = False
    retrieved: list[dict[str, Any]] = None  # type: ignore[assignment]
    status: str = "answer"
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.citations is None:
            self.citations = []
        if self.retrieved is None:
            self.retrieved = []


def _answered() -> _Result:
    return _Result(
        answer="Two-way crossover [PSG_1, p.4].",
        citations=[_Citation("PSG_1", 4, "c1", 11, 21, 0.88)],
        retrieved=[
            {
                "chunk_id": "c1",
                "doc_id": 11,
                "version_id": 21,
                "page": 4,
                "short_name": "PSG_1",
                "score": 0.88,
                "text": "should not reach the artifact",
            }
        ],
    )


def test_trace_records_retrieved_citations_and_answer() -> None:
    """Without the trace a scorecard cannot be audited: you can see that a metric
    moved but not which passages moved it."""
    item = GoldItem(question="q?", expected_sources=[{"short_name": "PSG_1", "page": 4}])
    sc = evaluate([item], ask_callable=lambda _q: _answered())
    trace = sc.details[0]["trace"]
    assert trace["retrieved"] == [
        {
            "chunk_id": "c1",
            "doc_id": 11,
            "version_id": 21,
            "page": 4,
            "short_name": "PSG_1",
            "score": 0.88,
        }
    ]
    assert trace["citations"][0]["chunk_id"] == "c1"
    assert trace["answer"] == "Two-way crossover [PSG_1, p.4]."
    assert trace["status"] == "answer"
    # _Result carries no claim_tags -- the trace must report that as empty,
    # not raise (see eval/metrics._trace's getattr default).
    assert trace["claim_tags"] == []


def test_trace_excludes_passage_and_snippet_text() -> None:
    """Ids, pages and scores make a finding checkable; full text would bloat the
    artifact past the point where anyone opens it."""
    item = GoldItem(question="q?", expected_sources=[{"short_name": "PSG_1", "page": 4}])
    sc = evaluate([item], ask_callable=lambda _q: _answered())
    trace = sc.details[0]["trace"]
    assert "text" not in trace["retrieved"][0]
    assert "snippet" not in trace["citations"][0]


def test_trace_is_recorded_for_refused_items_too() -> None:
    """A refusal is a decision worth auditing: it must show what it had in hand."""
    refused = _Result(answer="", refused=True, status="refused", reason="no_product")
    item = GoldItem(question="unknown drug?", expected_sources=[], must_refuse=True)
    sc = evaluate([item], ask_callable=lambda _q: refused)
    trace = sc.details[0]["trace"]
    assert trace["status"] == "refused"
    assert trace["reason"] == "no_product"
    assert trace["retrieved"] == []


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    scorecard: Any,
    check_thresholds: bool = True,
    persist: bool = True,
) -> Any:
    """Drive the real CLI entry point with the corpus and the LLM stubbed out.

    Everything downstream of `evaluate` is the real code path: fingerprint,
    artifact assembly, ledger write, threshold exit.
    """
    from regwatch.eval import ledger

    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        '{"question": "q?", "expected_sources": [{"short_name": "PSG_1", "page": 4}]}\n'
    )

    monkeypatch.setattr(run_eval, "init_db", lambda: None)
    monkeypatch.setattr(run_eval, "collection_size", lambda: 5)
    monkeypatch.setattr(run_eval, "evaluate", lambda *_a, **_k: scorecard)
    # These cases are about the ledger and the threshold exit, not about gold-set
    # correctness. The verifier needs a real corpus to check quotes against, and it
    # has its own dedicated coverage (test_verify_gold.py, plus
    # test_cli_refuses_to_score_a_gold_set_that_does_not_match_the_corpus below).
    monkeypatch.setattr(run_eval, "_verify_gold", lambda _items: None)

    before = len(ledger.recent_eval_runs("legacy", limit=100))
    try:
        run_eval.run(
            gold=gold,
            check_thresholds=check_thresholds,
            out=None,
            persist=persist,
            profile="legacy",
        )
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, before, ledger.recent_eval_runs("legacy", limit=100)


def test_cli_refuses_to_score_a_gold_set_that_does_not_match_the_corpus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A scorecard from a mis-pinned gold set is noise wearing a number.

    Verification runs BEFORE evaluate(), so a broken asset costs zero LLM calls --
    and cannot silently produce a number someone then trusts.
    """
    import regwatch.store.vector_store as vs
    from regwatch.eval.metrics import Scorecard

    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        '{"question": "q?", "category": "current_version", "expected_sources": '
        '[{"short_name": "PSG_1", "page": 6, "quote": "two-way crossover"}]}\n'
    )
    # The corpus has that phrase on page 4, not the page the row claims.
    monkeypatch.setattr(
        vs, "chunk_texts_at", lambda s, p: ["two-way crossover"] if p == 4 else ["other text"]
    )
    monkeypatch.setattr(run_eval, "init_db", lambda: None)
    monkeypatch.setattr(run_eval, "collection_size", lambda: 5)

    def _must_not_run(*_a: Any, **_k: Any) -> Scorecard:
        raise AssertionError("evaluate() must not run against an unverified gold set")

    monkeypatch.setattr(run_eval, "evaluate", _must_not_run)

    with pytest.raises(SystemExit) as exc:
        run_eval.run(gold=gold, check_thresholds=True, out=None, persist=True, profile="legacy")
    assert exc.value.code == 2


def test_cli_records_a_passing_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from regwatch.eval.metrics import Scorecard

    sc = Scorecard(
        n=1,
        recall_at_k=1.0,
        mrr=1.0,
        citation_precision=1.0,
        faithfulness=1.0,
        fact_recall=1.0,
        refusal_accuracy=1.0,
    )
    code, before, after = _run_cli(monkeypatch, tmp_path, scorecard=sc)
    assert code == 0
    assert len(after) == before + 1
    assert after[0]["passed"] is True
    assert after[0]["mrr"] == 1.0


def test_cli_records_a_failing_run_and_still_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The row a regression investigation needs must survive the gate failing."""
    from regwatch.eval.metrics import Scorecard

    sc = Scorecard(
        n=1,
        recall_at_k=0.0,
        mrr=0.0,
        citation_precision=0.0,
        faithfulness=1.0,
        fact_recall=1.0,
        refusal_accuracy=0.0,
    )
    code, before, after = _run_cli(monkeypatch, tmp_path, scorecard=sc)
    assert code == 2
    assert len(after) == before + 1
    assert after[0]["passed"] is False


def test_cli_no_persist_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from regwatch.eval.metrics import Scorecard

    sc = Scorecard(n=1, recall_at_k=1.0, mrr=1.0, citation_precision=1.0, refusal_accuracy=1.0)
    _code, before, after = _run_cli(monkeypatch, tmp_path, scorecard=sc, persist=False)
    assert len(after) == before


def test_cli_ledger_failure_does_not_change_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Bookkeeping must not be able to turn a red gate green, or a green gate red."""
    from regwatch.eval.metrics import Scorecard

    def _boom(**_kw: Any) -> int:
        raise RuntimeError("ledger down")

    monkeypatch.setattr("regwatch.eval.ledger.record_eval_run", _boom)
    passing = Scorecard(
        n=1,
        recall_at_k=1.0,
        mrr=1.0,
        citation_precision=1.0,
        faithfulness=1.0,
        fact_recall=1.0,
        refusal_accuracy=1.0,
    )
    code, _before, _after = _run_cli(monkeypatch, tmp_path, scorecard=passing)
    assert code == 0

    failing = Scorecard(n=1, recall_at_k=0.0, mrr=0.0, citation_precision=0.0, refusal_accuracy=0.0)
    code, _before, _after = _run_cli(monkeypatch, tmp_path, scorecard=failing)
    assert code == 2


def test_apply_profile_legacy_configures_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVE_EMBEDDING_PROFILE", "ep_stale_value")
    get_settings.cache_clear()
    try:
        assert run_eval._apply_profile("legacy") == "legacy"
        assert get_settings().active_embedding_profile == "legacy"
    finally:
        get_settings.cache_clear()


def test_apply_profile_rejects_an_arm_that_did_not_take_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the setting does not follow the flag, the run would silently score the
    wrong arm and the artifact would claim the right one."""

    class _Frozen:
        active_embedding_profile = "legacy"

    def _frozen_settings() -> _Frozen:
        return _Frozen()

    _frozen_settings.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(run_eval, "get_settings", _frozen_settings)
    # Well-formed on purpose: a malformed id would be rejected by the shape
    # check first and never reach the did-it-take-effect assertion.
    with pytest.raises(SystemExit) as excinfo:
        run_eval._apply_profile("ep_" + "a" * 32)
    assert "did not take effect" in str(excinfo.value)


def _patch_readiness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    complete: bool,
    pending: int,
    index_ready: bool,
) -> None:
    """Patch the two REAL functions the readiness check calls.

    Coverage and index readiness are separate calls in the store module --
    ProfileEmbeddingCoverage carries no index_ready field. A fake coverage
    object with an invented attribute passes whatever the caller reads off it,
    which is how a check that rejected every profile arm went unnoticed.
    """

    @dataclass
    class _Coverage:
        complete: bool
        pending_chunks: int

    import regwatch.store.embedding_profiles as profiles_mod

    assert not hasattr(
        profiles_mod.ProfileEmbeddingCoverage, "index_ready"
    ), "coverage grew an index_ready field; readiness must still use the real probe"
    monkeypatch.setattr(
        profiles_mod, "profile_embedding_coverage", lambda _p: _Coverage(complete, pending)
    )
    monkeypatch.setattr(profiles_mod, "profile_hnsw_index_ready", lambda _p: index_ready)


def test_assert_profile_ready_rejects_incomplete_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial coverage degrades recall silently instead of erroring, so it must
    fail before the run spends LLM calls."""
    _patch_readiness(monkeypatch, complete=False, pending=12, index_ready=True)
    with pytest.raises(SystemExit) as excinfo:
        run_eval._assert_profile_ready("ep_x")
    assert "not fully embedded" in str(excinfo.value)


def test_assert_profile_ready_rejects_a_missing_index(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_readiness(monkeypatch, complete=True, pending=0, index_ready=False)
    with pytest.raises(SystemExit) as excinfo:
        run_eval._assert_profile_ready("ep_x")
    assert "no ready HNSW index" in str(excinfo.value)


def test_assert_profile_ready_accepts_a_complete_indexed_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The missing half of the check: a healthy arm must actually be ACCEPTED.
    Without this, a readiness probe that always says no passes every test."""
    _patch_readiness(monkeypatch, complete=True, pending=0, index_ready=True)
    run_eval._assert_profile_ready("ep_x")


def test_assert_profile_ready_is_a_no_op_for_legacy() -> None:
    run_eval._assert_profile_ready("legacy")


def test_apply_profile_rejects_a_malformed_id_before_boot() -> None:
    """A typo'd arm is operator error about a flag. Validated here it is one
    line; left to init_db it is a boot traceback that reads like a broken tool."""
    with pytest.raises(SystemExit) as excinfo:
        run_eval._apply_profile("ep_nope")
    assert "is not a usable arm" in str(excinfo.value)
    # And it must not have configured the process on its way out.
    import os as _os

    assert _os.environ.get("ACTIVE_EMBEDDING_PROFILE") != "ep_nope"


def test_git_state_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance is best-effort: it must degrade to 'unknown', never fail a run."""
    monkeypatch.setattr(
        run_fingerprint,
        "_git",
        lambda *_a: (_ for _ in ()).throw(OSError("no git here")),
    )
    with pytest.raises(OSError):
        run_fingerprint._git("rev-parse")  # the stub itself raises

    monkeypatch.setattr(run_fingerprint, "_git", lambda *_a: "")
    sha, dirty = run_fingerprint.git_state()
    assert sha == "unknown"
    assert dirty is False


# --- The ratchet has teeth --------------------------------------------------
#
# THRESHOLDS was lowered on 2026-08-05 from aspirational figures (0.90/0.95/0.95,
# written against echo embeddings) to a ratchet just under the first real
# measurement. A lowered gate is only defensible if it still fails on a real
# regression, so these drive the actual CLI exit path with the REAL THRESHOLDS
# dict -- not a copy -- so editing that dict re-runs these assertions against it.


def _sc(**over: float) -> Any:
    """A scorecard at the recorded 2026-08-05 baseline, overridable per case.

    refusal_accuracy is that run's number RE-SCORED under the withhold policy
    (issue #161): the recorded scorecard artifact says 0.710 because it scored
    the status string, and re-scoring its 62 rows with metrics.withheld_answer
    gives 0.903. Same run, same replies -- only the predicate changed.
    """
    from regwatch.eval.metrics import Scorecard

    base = {
        "n": 62,
        "recall_at_k": 0.814,
        "mrr": 0.506,
        "citation_precision": 0.756,
        "faithfulness": 0.826,
        "fact_recall": 0.622,
        "refusal_accuracy": 0.903,
    }
    base.update(over)
    return Scorecard(**base)  # type: ignore[arg-type]


def test_recorded_baseline_passes_the_ratchet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Green today -- but see the tests below for why that is not vacuous."""
    code, _, _ = _run_cli(monkeypatch, tmp_path, scorecard=_sc(), persist=False)
    assert code == 0


def test_broken_retrieval_still_fails_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The case the gate exists for: retrieval returns nothing relevant.

    This is C1's acceptance criterion and the reason the ratchet is a ratchet
    rather than a removal. If this ever exits 0, the eval has become decorative.
    """
    code, _, _ = _run_cli(
        monkeypatch,
        tmp_path,
        scorecard=_sc(recall_at_k=0.0, citation_precision=0.0, mrr=0.0),
        persist=False,
    )
    assert code == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # Just under each blocking floor: the smallest regression that must trip.
        ("recall_at_k", 0.79),
        # Tracks the floor: 0.73 sat under the old 0.74 but clears the 0.70
        # prose-era floor, so it would no longer trip the gate it exists to
        # prove trips.
        ("citation_precision", 0.69),
    ],
)
def test_a_regression_below_any_blocking_floor_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, field: str, value: float
) -> None:
    code, _, _ = _run_cli(monkeypatch, tmp_path, scorecard=_sc(**{field: value}), persist=False)
    assert code == 2, f"{field}={value} is below its floor and must fail the build"


def test_refusal_accuracy_is_measured_but_not_blocking() -> None:
    """Un-gated 2026-08-06 by owner decision, and deliberately pinned.

    Not the earlier "labels are disputed" reason -- that was settled (issue
    #161) and metrics.withheld_answer still enforces it. The product is moving
    to a conversational Ask layer that is not meant to refuse, so gating on how
    often it declines would fail the build for doing the new thing correctly.
    The metric and its 16 gold rows are slated for removal; until then it stays
    measured, printed and persisted so the transition is visible.

    If refusal_accuracy is ever reintroduced to THRESHOLDS, THIS TEST SHOULD
    FAIL -- that is the intended signal to delete it and record why.
    """
    from regwatch.eval.run_eval import TARGETS, THRESHOLDS

    assert "refusal_accuracy" not in THRESHOLDS
    assert TARGETS["refusal_accuracy"] == 0.95


def test_a_collapsed_refusal_score_no_longer_fails_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The un-gating, driven through the real CLI exit path.

    0.0 is the extreme: every decision wrong. It must still exit 0, because the
    number is reported rather than enforced.
    """
    code, _, _ = _run_cli(monkeypatch, tmp_path, scorecard=_sc(refusal_accuracy=0.0), persist=False)
    assert code == 0


def test_every_blocking_floor_sits_below_its_target(_unused: None = None) -> None:
    """A gate above its own target would be incoherent."""
    from regwatch.eval.run_eval import TARGETS, THRESHOLDS

    for metric, floor in THRESHOLDS.items():
        assert metric in TARGETS, f"blocking metric {metric} has no recorded target"
        assert (
            floor <= TARGETS[metric]
        ), f"{metric} gate {floor} exceeds its target {TARGETS[metric]}"


def test_a_run_that_could_not_measure_fails_differently_from_one_that_measured_badly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Exit 3, not 2, and not a pass.

    Transport-failed turns leave every denominator, so without this guard a
    provider outage could shrink the gate to a few lucky rows and report a
    green build. "Could not measure" and "measured badly" are different
    failures and CI must be able to tell them apart -- conflating them is what
    sent two PRs red on 2026-08-06 with no code regression behind it.
    """
    from regwatch.eval.run_eval import MAX_UNMEASURED_FRACTION

    assert MAX_UNMEASURED_FRACTION == 0.10

    # 5/62 = 8%: the recorded rate-limit run. Under the cap, so its metrics
    # stand and the gate scores them normally.
    ok, _, _ = _run_cli(monkeypatch, tmp_path, scorecard=_sc(errored=5), persist=False)
    assert ok == 0

    # 7/62 = 11%: over the cap. The metrics are meaningless, so refuse to score.
    broken, _, _ = _run_cli(monkeypatch, tmp_path, scorecard=_sc(errored=7), persist=False)
    assert broken == 3, "an unmeasurable run must not exit 0 (pass) or 2 (regression)"


def test_the_unmeasured_guard_outranks_a_threshold_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """When both are true the message must name the real problem.

    A run this broken has metrics computed over too few rows to mean anything,
    so reporting "recall regressed" would send someone hunting a retrieval bug
    that is not there.
    """
    code, _, _ = _run_cli(
        monkeypatch,
        tmp_path,
        scorecard=_sc(errored=20, recall_at_k=0.0, citation_precision=0.0),
        persist=False,
    )
    assert code == 3
