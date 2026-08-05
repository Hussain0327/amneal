"""Store tests for the eval_run ledger.

The ledger's whole value is that it records the runs you would rather forget:
a failing gate and a dirty tree are exactly the rows a later investigation
needs. These tests pin that, plus the two provenance fields that stop a trend
line from silently splicing incomparable runs (gold-set hash, dirty flag).
"""

from __future__ import annotations

from pathlib import Path

from regwatch.eval.ledger import (
    gold_set_sha256,
    recent_eval_runs,
    record_eval_run,
    scorecard_passed,
)
from regwatch.eval.metrics import Scorecard
from regwatch.eval.run_fingerprint import CorpusDigest, RunFingerprint

_THRESHOLDS = {"recall_at_k": 0.90, "citation_precision": 0.95, "refusal_accuracy": 0.95}


def _fingerprint(profile: str = "legacy", *, dirty: bool = False) -> RunFingerprint:
    return RunFingerprint(
        profile=profile,
        corpus=CorpusDigest(chunks=5494, docs=1795, digest="d" * 32),
        commit="c" * 40,
        dirty=dirty,
    )


def _scorecard(**over: float) -> Scorecard:
    base = {
        "n": 12,
        "recall_at_k": 1.0,
        "mrr": 0.75,
        "citation_precision": 1.0,
        "faithfulness": 1.0,
        "fact_recall": 1.0,
        "refusal_accuracy": 1.0,
    }
    base.update(over)
    return Scorecard(**base)  # type: ignore[arg-type]


def _record(fp: RunFingerprint, sc: Scorecard, gold: Path) -> int | None:
    return record_eval_run(
        fingerprint=fp,
        scorecard=sc,
        thresholds=_THRESHOLDS,
        gold_path=gold,
        artifact={"scorecard": {"n": sc.n}},
    )


def test_records_run_with_metrics(tmp_path: Path) -> None:
    gold = tmp_path / "gold.jsonl"
    gold.write_text('{"question": "q"}\n')
    run_id = _record(_fingerprint(), _scorecard(), gold)
    assert run_id is not None

    rows = recent_eval_runs("legacy")
    assert rows[0]["id"] == run_id
    assert rows[0]["mrr"] == 0.75
    assert rows[0]["corpus_chunks"] == 5494
    assert rows[0]["passed"] is True
    assert rows[0]["gold_set_sha256"] == gold_set_sha256(gold)


def test_failing_run_is_still_recorded(tmp_path: Path) -> None:
    """The row a regression investigation actually needs."""
    gold = tmp_path / "gold.jsonl"
    gold.write_text("{}\n")
    run_id = _record(_fingerprint("ep_" + "a" * 32), _scorecard(recall_at_k=0.10), gold)
    assert run_id is not None

    rows = recent_eval_runs("ep_" + "a" * 32)
    assert rows[0]["passed"] is False
    assert rows[0]["recall_at_k"] == 0.10


def test_dirty_tree_is_recorded(tmp_path: Path) -> None:
    gold = tmp_path / "gold.jsonl"
    gold.write_text("{}\n")
    _record(_fingerprint("ep_" + "b" * 32, dirty=True), _scorecard(), gold)
    assert recent_eval_runs("ep_" + "b" * 32)[0]["dirty"] is True


def test_recent_runs_are_scoped_to_one_profile(tmp_path: Path) -> None:
    """Two arms have different embedding geometry: their scores are not one trend."""
    gold = tmp_path / "gold.jsonl"
    gold.write_text("{}\n")
    left = "ep_" + "c" * 32
    right = "ep_" + "d" * 32
    _record(_fingerprint(left), _scorecard(mrr=0.10), gold)
    _record(_fingerprint(right), _scorecard(mrr=0.90), gold)

    assert [r["mrr"] for r in recent_eval_runs(left)] == [0.10]
    assert [r["mrr"] for r in recent_eval_runs(right)] == [0.90]


def test_recent_runs_newest_first(tmp_path: Path) -> None:
    gold = tmp_path / "gold.jsonl"
    gold.write_text("{}\n")
    profile = "ep_" + "e" * 32
    first = _record(_fingerprint(profile), _scorecard(mrr=0.1), gold)
    second = _record(_fingerprint(profile), _scorecard(mrr=0.2), gold)

    ids = [r["id"] for r in recent_eval_runs(profile)]
    assert ids == [second, first]


def test_gold_set_hash_tracks_file_bytes(tmp_path: Path) -> None:
    """A comment-only edit still changes the hash: reviewers see the file, not the parse."""
    gold = tmp_path / "gold.jsonl"
    gold.write_text('{"question": "q"}\n')
    before = gold_set_sha256(gold)
    gold.write_text('# note\n{"question": "q"}\n')
    assert gold_set_sha256(gold) != before


def test_gold_set_hash_missing_file_is_empty(tmp_path: Path) -> None:
    """Provenance must never be the thing that breaks a run."""
    assert gold_set_sha256(tmp_path / "absent.jsonl") == ""


def test_scorecard_passed_matches_every_gated_metric() -> None:
    assert scorecard_passed(_scorecard(), _THRESHOLDS) is True
    # One metric below its threshold is enough to fail the gate.
    assert scorecard_passed(_scorecard(refusal_accuracy=0.94), _THRESHOLDS) is False
    # A metric with no threshold cannot fail it.
    assert scorecard_passed(_scorecard(mrr=0.0), _THRESHOLDS) is True
