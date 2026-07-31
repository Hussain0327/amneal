"""Deficiency analysis runner: parse -> split -> detect -> persist.

Replaces upstream DefPredict's ``agents/orchestrator.py``: same pipeline
sequence, but job state lives in ``deficiency_run`` (store/deficiency_runs)
instead of a Delta table, and completion is gated on the audit write.

Runs synchronously inside a worker thread; the API layer owns the capacity
limiter, the timeout, and the temp-file lifecycle. State transitions are
compare-and-set, so this function racing the API's timeout writer is safe:
whoever transitions first wins and the loser no-ops (see store/deficiency_runs).

INV-6 (no-audit-no-answer): the QueryLog row (mode="defpredict") is written
BEFORE the run flips to complete. If the audit write fails, the run is marked
failed and the report is discarded -- report content is only ever served from
a completed row, so an unaudited answer cannot reach the UI.
"""

from __future__ import annotations

import time
from typing import Any

from regwatch.common.audit import log_query
from regwatch.common.logging import get_logger
from regwatch.common.observability import capture_exception
from regwatch.deficiency.detection import run_detection
from regwatch.deficiency.events import emit_sync
from regwatch.deficiency.parse.pdf import extract_pdf
from regwatch.deficiency.parse.section_splitter import group_sections, split_document
from regwatch.generate.llm import current_model_name
from regwatch.store.deficiency_runs import claim_running, complete_run, fail_run, get_run

log = get_logger(__name__)


def _tier_counts(report: Any) -> dict[str, int]:
    counts = {"verified": 0, "corroborated": 0, "advisory": 0}
    for fault in report.faults:
        tier = getattr(fault.tier, "value", str(fault.tier))
        if tier in counts:
            counts[tier] += 1
    return counts


def run_deficiency_analysis(run_id: int, pdf_path: str) -> None:
    """Execute one analysis job end to end. Never raises: every failure path
    records a terminal run state instead (this runs as a background task with
    nobody left to catch)."""
    run = get_run(run_id)
    if run is None:
        log.error("deficiency_run_missing", run_id=run_id)
        return
    if not claim_running(run_id):
        # The timeout writer beat us to a terminal state before we even
        # started (limiter queue longer than the deadline). Nothing to do.
        return

    job_id = str(run_id)
    start = time.time()
    emit_sync(job_id, "detection", "pipeline_start", "Runner", "Starting analysis pipeline")
    try:
        doc = extract_pdf(pdf_path)
        sections = split_document(doc)
        groups = group_sections(sections)
        log.info(
            "deficiency_parsed",
            run_id=run_id,
            pages=doc.get("page_count"),
            sections=len(sections),
            groups=len(groups),
        )
        report = run_detection(doc, sections, groups, job_id=job_id)
        report.job_id = job_id
        report.analysis_seconds = round(time.time() - start, 1)
        payload = report.model_dump(mode="json")
        counts = _tier_counts(report)
        summary = (
            f"Deficiency analysis of {run.filename}: {len(report.faults)} candidate "
            f"deficiencies ({counts['verified']} verified, {counts['corroborated']} "
            f"corroborated, {counts['advisory']} advisory)."
        )
        # Audit BEFORE completion: if this raises, the except path fails the
        # run and the report never becomes servable (INV-6).
        audit_id = log_query(
            mode="defpredict",
            query_text=f"deficiency analyze filename={run.filename!r} sha256={run.sha256}",
            retrieved=[],
            answer_text=summary,
            citations=[],
            refused=False,
            model_name=current_model_name(),
            user_id=str(run.created_by_user_id),
            status="complete",
            route_json={
                "route": "defpredict",
                "run_id": run_id,
                "fault_count": len(report.faults),
                "tier_counts": counts,
                "page_count": doc.get("page_count"),
            },
            latency_ms=int((time.time() - start) * 1000),
        )
        complete_run(
            run_id,
            report=payload,
            fault_count=len(report.faults),
            page_count=int(doc.get("page_count") or 0) or None,
            audit_id=audit_id,
        )
        emit_sync(
            job_id,
            "detection",
            "pipeline_complete",
            "Runner",
            f"Analysis complete in {report.analysis_seconds:.1f}s -- "
            f"{len(report.faults)} faults",
        )
    except Exception as exc:
        # D1ResidencyError lands here too: the run fails loudly with the
        # residency message and a Sentry capture -- never a silent fallback.
        log.error(
            "deficiency_run_failed",
            run_id=run_id,
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        capture_exception(exc)
        audit_id = None
        try:
            audit_id = log_query(
                mode="defpredict",
                query_text=f"deficiency analyze filename={run.filename!r} sha256={run.sha256}",
                retrieved=[],
                answer_text="",
                citations=[],
                refused=False,
                model_name=current_model_name(),
                user_id=str(run.created_by_user_id),
                status="error",
                route_json={
                    "route": "defpredict",
                    "run_id": run_id,
                    "error_type": type(exc).__name__,
                },
                latency_ms=int((time.time() - start) * 1000),
            )
        except Exception as audit_exc:
            # DEFINED failure: the run row still records the terminal state;
            # a raising audit write must not mask the original error.
            log.warning(
                "deficiency_audit_write_failed",
                run_id=run_id,
                error_type=type(audit_exc).__name__,
            )
            capture_exception(audit_exc)
        fail_run(run_id, error=f"{type(exc).__name__}: {exc}", audit_id=audit_id)
