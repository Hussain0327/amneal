"""The fault-detection layer entry point.

Runs the four stages over the structured document and returns a FaultReport:
  Stage 1 oracles + Stage 2 checklists (deterministic, unconditional)  |
  Stage 3 selection -> specialists + open reviewers (parallel)         +--> Stage 4 verify+tier
"""

from __future__ import annotations

import time

from regwatch.common.logging import get_logger
from regwatch.deficiency.detection.catalog import CANONICAL_DOMAINS
from regwatch.deficiency.detection.challenge import challenge_faults
from regwatch.deficiency.detection.checklists import run_checklists
from regwatch.deficiency.detection.ctd import describe_document, detect_ctd_section
from regwatch.deficiency.detection.oracles import run_oracles
from regwatch.deficiency.detection.selection import gather_precedents, select_domains
from regwatch.deficiency.detection.subagents import run_subagents
from regwatch.deficiency.detection.verify import verify_and_tier
from regwatch.deficiency.events import emit_sync
from regwatch.deficiency.schemas.faults import FaultReport

log = get_logger(__name__)


def _leading_text(doc: dict, pages: int = 3) -> str:
    parts: list[str] = []
    for page in doc.get("pages", [])[:pages]:
        for block in page.get("blocks", []):
            parts.append(block.get("text") or "")
    return " ".join(parts)


def run_detection(
    doc: dict, sections: list[dict], groups: list[dict], job_id: str = ""
) -> FaultReport:
    start = time.time()
    ctd = detect_ctd_section(_leading_text(doc) or doc.get("filename", ""))
    doc_desc = describe_document(ctd)
    emit_sync(job_id, "detection", "layer_start", "Detection", f"Reviewing {doc_desc}")

    # Stage 1 + 2 -- deterministic, run unconditionally on the full doc.
    oracle_faults = run_oracles(doc)
    checklist_faults = run_checklists(doc, ctd)
    emit_sync(
        job_id,
        "detection",
        "oracle_complete",
        "Oracles",
        f"{len(oracle_faults)} code-verified, {len(checklist_faults)} checklist findings",
    )

    # Stage 3 -- selection (adaptive) then the sub-agent fan-out.
    domains = select_domains(doc, sections)
    emit_sync(
        job_id, "detection", "selection", "Selector", f"Domains: {', '.join(domains) or 'none'}"
    )
    precedents = {d: gather_precedents(d, doc) for d in domains}
    for d in domains:
        emit_sync(
            job_id,
            "detection",
            "agent_spawned",
            f"specialist:{d}",
            CANONICAL_DOMAINS.get(d, "")[:80],
        )
    for g in groups:
        emit_sync(
            job_id,
            "detection",
            "agent_spawned",
            f"reviewer:{g.get('group_id', '')}",
            "Open review of this region",
        )
    agent_faults, failures = run_subagents(sections, groups, domains, precedents, doc_desc)

    # Stage 4 -- verify + tier + dedup, then the grounded challenge (scores, never vetoes).
    faults = verify_and_tier(oracle_faults + checklist_faults + agent_faults, doc)
    faults = challenge_faults(faults, sections, doc)
    emit_sync(job_id, "detection", "agent_message", "Challenge", "Grounded-challenge pass complete")
    emit_sync(
        job_id,
        "detection",
        "layer_complete",
        "Detection",
        f"{len(faults)} faults surfaced ({len(failures)} agent parse failures)",
    )
    log.info(
        "detection_complete",
        faults=len(faults),
        domains=len(domains),
        seconds=round(time.time() - start, 1),
    )

    return FaultReport(
        job_id=job_id,
        faults=faults,
        faults_found=bool(faults),
        domains_checked=domains,
        parse_failures=failures,
        analysis_seconds=round(time.time() - start, 1),
    )
