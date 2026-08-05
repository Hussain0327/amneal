"""Durable eval-run ledger: persist + read one row per COMPLETED eval run.

WHY: a scorecard is only evidence when it is comparable to another scorecard.
Before this ledger a run existed as terminal output plus an optional ``--out``
JSON file uploaded to a CI run that ages out, so "did the chunker change hurt
recall?" could not be answered from the repository -- only from someone's
memory of a number in a PR comment.

Recording mirrors ``watch/runs.py`` deliberately (INV-4 -- never report a run
state that did not happen):
  * a run that COMPLETES records a row, INCLUDING a run that fails the gate
    (``passed=False``): a failing eval is a real measurement and is precisely
    the row a later investigation needs;
  * a run that RAISES before scoring records NOTHING -- there is no scorecard
    to record, and a row would claim a measurement that never finished.

Persistence never fails the eval. The measurement is the product; the ledger
write is bookkeeping on top of it, and a DB hiccup must not turn a green gate
red (or, worse, a red gate green by aborting before the threshold check).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import desc
from sqlmodel import select

from regwatch.eval.metrics import Scorecard
from regwatch.eval.run_fingerprint import RunFingerprint
from regwatch.store.db import session_scope
from regwatch.store.models import EvalRun

_METRIC_FIELDS = (
    "recall_at_k",
    "mrr",
    "citation_precision",
    "faithfulness",
    "fact_recall",
    "refusal_accuracy",
)


def gold_set_sha256(path: Path) -> str:
    """Hash the gold set BYTES, not the parsed items.

    Comments and ordering are part of what a reviewer sees, and a hash over the
    parsed model would call two visibly different files identical. A missing
    file hashes to "" rather than raising: the caller has already failed on it
    if it matters, and provenance must never be the thing that breaks a run.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def scorecard_passed(sc: Scorecard, thresholds: dict[str, float]) -> bool:
    """Whether every gated metric cleared its threshold.

    Same predicate the CLI exits on, in one place so the stored ``passed`` flag
    can never disagree with the exit code.
    """
    return all(getattr(sc, key) >= thr for key, thr in thresholds.items())


def record_eval_run(
    *,
    fingerprint: RunFingerprint,
    scorecard: Scorecard,
    thresholds: dict[str, float],
    gold_path: Path,
    artifact: dict[str, Any],
) -> int | None:
    """Persist one completed eval run. Returns the row id, or None if not stored.

    Own ``session_scope``, and never raises: see the module docstring. The
    caller reports what happened rather than dying on it.
    """
    row = EvalRun(
        profile_id=fingerprint.profile,
        commit_sha=fingerprint.commit,
        dirty=fingerprint.dirty,
        gold_set_sha256=gold_set_sha256(gold_path),
        n_items=scorecard.n,
        corpus_chunks=fingerprint.corpus.chunks,
        corpus_docs=fingerprint.corpus.docs,
        passed=scorecard_passed(scorecard, thresholds),
        artifact_json=artifact,
        **{key: float(getattr(scorecard, key)) for key in _METRIC_FIELDS},
    )
    with session_scope() as s:
        s.add(row)
        # Flush inside the scope so the generated id is available; commit
        # happens on scope exit.
        s.flush()
        return row.id


def recent_eval_runs(profile_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """The newest runs for one arm, newest first.

    Scoped to a single profile because that is the only comparison that means
    anything: two arms have different embedding geometry, so their scorecards
    are not points on one trend line. Materialized INSIDE the session --
    expire_on_commit detaches rows on scope exit.
    """
    with session_scope() as s:
        rows = s.scalars(
            select(EvalRun)
            .where(EvalRun.profile_id == profile_id)
            .order_by(desc(EvalRun.created_at), desc(EvalRun.id))  # type: ignore[arg-type]
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "profile_id": r.profile_id,
                "commit_sha": r.commit_sha,
                "dirty": r.dirty,
                "gold_set_sha256": r.gold_set_sha256,
                "n_items": r.n_items,
                "corpus_chunks": r.corpus_chunks,
                "corpus_docs": r.corpus_docs,
                "passed": r.passed,
                **{key: getattr(r, key) for key in _METRIC_FIELDS},
            }
            for r in rows
        ]


def scorecard_to_dict(sc: Scorecard) -> dict[str, Any]:
    """Scorecard as a plain dict (helper so callers don't import dataclasses)."""
    return asdict(sc)
