"""Stage 3, part 2 -- the sub-agent fan-out.

Two orthogonal agent kinds, same 70B, isolated context, run in parallel:
  * domain specialists -- one domain, whole doc (entity-scoped), precedent-seeded
  * open reviewers     -- one section-group, every possible fault (the novel-fault path)

Agents are the budget, not the fault ceiling: each returns as many findings as it finds.
No group chat, no consensus -- the orchestrator gathers, Stage 4 verifies and tiers.
"""

from __future__ import annotations

import concurrent.futures

from pydantic import BaseModel, Field

from regwatch.common.logging import get_logger
from regwatch.deficiency.detection.catalog import CANONICAL_DOMAINS
from regwatch.deficiency.detection.prompts import OPEN_REVIEWER, SPECIALIST
from regwatch.deficiency.detection.render import render_sections
from regwatch.deficiency.schemas.faults import EvidenceClass, Fault, Tier
from regwatch.deficiency.schemas.flaws import Severity, SimilarDeficiency
from regwatch.deficiency.schemas.llm import ParseFailed
from regwatch.deficiency.structured import structured_call
from regwatch.generate.llm import D1ResidencyError

log = get_logger(__name__)

_MAX_WORKERS = 6

_DOMAIN_HINTS: dict[str, set[str]] = {
    "method-validation": {
        "validation",
        "linearity",
        "precision",
        "specificity",
        "suitability",
        "lod",
        "loq",
        "accuracy",
    },
    "impurities": {"impurit", "related"},
    "elemental-impurities": {"elemental", "q3d", "metal"},
    "residual-solvents": {"residual", "solvent", "q3c"},
    "stability": {"stability", "shelf"},
    "dissolution": {"dissolution", "ivrt"},
    "container-closure": {"leachable", "extractable", "container", "closure"},
    "specification": {"specification", "acceptance", "assay", "coa"},
}


class RawFinding(BaseModel):
    title: str = Field(description="One-line statement of the deficiency.")
    detail: str = Field(default="", description="What is wrong and why, argued from the evidence.")
    evidence: str = Field(
        default="", description="Verbatim value, cell, or sentence from the document."
    )
    section: str = Field(default="", description="Section heading or number the fault sits in.")
    page: int = 0
    table_ref: str = Field(
        default="", description="Table it concerns, e.g. 'Table 16'; empty if none."
    )
    severity: Severity = Severity.MEDIUM


class AgentFindings(BaseModel):
    findings: list[RawFinding] = Field(default_factory=list)


def _precedent_block(precedents: list[SimilarDeficiency]) -> str:
    if not precedents:
        return "No close historical precedent was retrieved -- rely on guidance and the document itself."
    lines = ["Historical ANDA deficiencies of this kind (reference only, do not copy):"]
    for p in precedents[:3]:
        lines.append(f"- [{p.product_name or 'unknown'}] {p.deficiency_text[:280]}")
    return "\n".join(lines)


def _scope_for_domain(domain: str, sections: list[dict]) -> str:
    terms = set(domain.replace("-", " ").split()) | _DOMAIN_HINTS.get(domain, set())
    picked = [
        s
        for s in sections
        if any(t in (s.get("heading", "") + " " + s.get("text", "")).lower() for t in terms)
    ]
    return render_sections(picked or sections)


def _to_faults(
    inst: AgentFindings, source: str, precedents: list[SimilarDeficiency]
) -> list[Fault]:
    faults: list[Fault] = []
    for f in inst.findings:
        if not (f.title or "").strip():
            continue
        faults.append(
            Fault(
                title=f.title.strip(),
                detail=f.detail,
                severity=f.severity,
                evidence=f.evidence,
                section=f.section,
                page=f.page,
                table_ref=f.table_ref,
                source=source,
                precedents=list(precedents),
                evidence_class=EvidenceClass.MODEL_JUDGMENT,
                tier=Tier.ADVISORY,
            )
        )
    return faults


def _run_specialist(
    domain: str, sections: list[dict], precedents: list[SimilarDeficiency], doc_desc: str
):
    system = SPECIALIST.format(
        domain=domain,
        domain_desc=CANONICAL_DOMAINS.get(domain, ""),
        doc_desc=doc_desc,
        precedents=_precedent_block(precedents),
    )
    content = _scope_for_domain(domain, sections)
    inst, failure = structured_call(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
        model_cls=AgentFindings,
        temperature=0.0,
        max_tokens=2048,
        repair_context=f"specialist:{domain}",
    )
    return (_to_faults(inst, f"specialist:{domain}", precedents) if inst else []), failure


def _run_reviewer(group: dict, doc_desc: str):
    label = group.get("group_id", "")
    content = render_sections(group.get("sections", []))
    inst, failure = structured_call(
        messages=[
            {"role": "system", "content": OPEN_REVIEWER},
            {"role": "user", "content": content},
        ],
        model_cls=AgentFindings,
        temperature=0.0,
        max_tokens=2048,
        repair_context=f"reviewer:{label}",
    )
    return (_to_faults(inst, f"reviewer:{label}", []) if inst else []), failure


def run_subagents(
    sections: list[dict],
    groups: list[dict],
    domains: list[str],
    precedents_by_domain: dict[str, list[SimilarDeficiency]],
    doc_desc: str,
) -> tuple[list[Fault], list[ParseFailed]]:
    faults: list[Fault] = []
    failures: list[ParseFailed] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = [
            pool.submit(_run_specialist, d, sections, precedents_by_domain.get(d, []), doc_desc)
            for d in domains
        ]
        futures += [pool.submit(_run_reviewer, g, doc_desc) for g in groups]
        for fut in concurrent.futures.as_completed(futures):
            try:
                fs, failure = fut.result()
                faults.extend(fs)
                if failure is not None:
                    failures.append(failure)
            except D1ResidencyError:
                # A residency violation must fail the run loudly, not be logged as one
                # more degraded sub-agent.
                raise
            except Exception as exc:
                log.warning("subagent_failed", error=str(exc)[:200])
    return faults, failures
