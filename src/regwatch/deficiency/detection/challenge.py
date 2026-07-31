"""Stage 4 -- the grounded challenge (find/contradict as a scorer, never a veto).

For each soft (non-oracle) finding, a challenger tries to REFUTE it using only the document.
A refutation counts only if it quotes a passage that actually resolves the concern AND that
passage is verbatim in the document -- ungrounded skepticism changes nothing. The challenge
only ever *lowers confidence* (and keeps the finding at advisory); it never drops a finding.
Only a deterministic oracle may suppress. This is what makes same-model agents safe: they
score against the document, they do not vote each other off.
"""

from __future__ import annotations

import concurrent.futures
import re

from pydantic import BaseModel, Field

from regwatch.common.logging import get_logger
from regwatch.deficiency.detection.prompts import CHALLENGE
from regwatch.deficiency.detection.render import render_sections
from regwatch.deficiency.detection.verify import _anchored, _doc_corpus
from regwatch.deficiency.schemas.faults import EvidenceClass, Fault, Tier
from regwatch.deficiency.structured import structured_call
from regwatch.generate.llm import D1ResidencyError

log = get_logger(__name__)

_MAX_CHALLENGES = 20
_MAX_WORKERS = 6

_TIER_ORDER = {Tier.VERIFIED: 0, Tier.CORROBORATED: 1, Tier.ADVISORY: 2}
_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


class ChallengeVerdict(BaseModel):
    refuted: bool = Field(description="True only if a resolving passage was found in the document.")
    counter_evidence: str = Field(
        default="", description="The exact passage that resolves the concern; empty if none."
    )
    reasoning: str = Field(default="", description="One sentence.")


_TABLE_NUM = re.compile(r"table\s*(\d+)", re.I)


def _table_num(text: str) -> str:
    m = _TABLE_NUM.search(text or "")
    return m.group(1) if m else ""


def _sections_for(fault: Fault, sections: list[dict]) -> list[dict]:
    """The sections the challenger needs: the one holding the referenced table, plus any whose
    heading matches the fault's section. Resolving table_ref is what lets the challenger see the
    evidence a reviewer's vague `section` field would otherwise hide (e.g. the N/A cell and the
    prose that explains it)."""
    picked: list[dict] = []
    ref_num = _table_num(fault.table_ref)
    if ref_num:
        for s in sections:
            if any(_table_num(t.get("title", "")) == ref_num for t in s.get("tables", [])):
                picked.append(s)
    if fault.section:
        needle = fault.section.lower()
        for s in sections:
            heading = (s.get("heading", "") or "").lower()
            if s not in picked and heading and (needle in heading or heading in needle):
                picked.append(s)
    return picked


def _context_for(fault: Fault, sections: list[dict]) -> str:
    picked = _sections_for(fault, sections)
    if picked:
        return render_sections(picked, char_budget=16_000)
    # No anchor to a section/table -> give the challenger broad access; its job is to refute,
    # so wide context here (unlike at detection) is correct.
    return render_sections(sections, char_budget=30_000)


def _apply_verdict(fault: Fault, verdict: ChallengeVerdict, corpus: str) -> None:
    """Confidence scorer, never a veto. A grounded refutation downgrades; otherwise the finding
    survives and gains a little confidence."""
    grounded = (
        verdict.refuted
        and verdict.counter_evidence.strip()
        and _anchored(verdict.counter_evidence, corpus)
    )
    if grounded:
        fault.confidence = round(fault.confidence * 0.4, 2)
        fault.tier = Tier.ADVISORY
        fault.challenge_note = verdict.counter_evidence.strip()[:300]
    else:
        fault.confidence = min(round(fault.confidence + 0.1, 2), 0.9)


def _challenge_one(fault: Fault, sections: list[dict]) -> ChallengeVerdict | None:
    context = _context_for(fault, sections)
    user = (
        "Proposed deficiency:\n"
        f"Title: {fault.title}\n"
        f"Detail: {fault.detail}\n"
        f"Evidence cited: {fault.evidence}\n\n"
        f"Document excerpt:\n{context}"
    )
    inst, _failure = structured_call(
        messages=[{"role": "system", "content": CHALLENGE}, {"role": "user", "content": user}],
        model_cls=ChallengeVerdict,
        temperature=0.0,
        max_tokens=512,
        repair_context="challenge",
    )
    return inst


def challenge_faults(faults: list[Fault], sections: list[dict], doc: dict) -> list[Fault]:
    """Run the grounded challenge on soft findings and re-sort by (tier, severity, confidence)."""
    corpus = _doc_corpus(doc)
    targets = [
        f
        for f in faults
        if f.evidence_class in (EvidenceClass.MODEL_JUDGMENT, EvidenceClass.QUOTE_ANCHORED)
    ][:_MAX_CHALLENGES]

    if targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            future_to_fault = {pool.submit(_challenge_one, f, sections): f for f in targets}
            for future in concurrent.futures.as_completed(future_to_fault):
                fault = future_to_fault[future]
                try:
                    verdict = future.result()
                    if verdict is not None:
                        _apply_verdict(fault, verdict, corpus)
                except D1ResidencyError:
                    # A residency violation must fail the run loudly; it is not a
                    # "failed challenge" the finding can survive.
                    raise
                except Exception as exc:  # a failed challenge must not drop the finding
                    log.warning("challenge_failed", error=str(exc)[:200])

    return sorted(
        faults,
        key=lambda f: (_TIER_ORDER[f.tier], _SEV_ORDER.get(f.severity.value, 1), -f.confidence),
    )
