"""Stage 4 -- typed verification, evidence-class stamping, confidence tiering, dedup.

Reconciles findings from all stages. Oracle/checklist findings keep their authority.
Sub-agent findings get their evidence anchored against the document (verbatim -> higher
confidence) and are tiered by whether real precedent backs them. Nothing is dropped here
except exact duplicates -- suppression is reserved for oracle-disproof, which happens by an
oracle simply not emitting the fault. Recall lives in the ADVISORY tier.
"""

from __future__ import annotations

import re

from regwatch.deficiency.schemas.faults import EvidenceClass, Fault, Tier
from regwatch.deficiency.schemas.flaws import Severity

_AUTHORITY = {
    EvidenceClass.CODE_VERIFIED: 3,
    EvidenceClass.CHECKLIST: 2,
    EvidenceClass.QUOTE_ANCHORED: 1,
    EvidenceClass.MODEL_JUDGMENT: 0,
}
_TIER_ORDER = {Tier.VERIFIED: 0, Tier.CORROBORATED: 1, Tier.ADVISORY: 2}
_SEV_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _doc_corpus(doc: dict) -> str:
    """One space-stripped blob of every cell/pair/block, for verbatim anchoring."""
    parts: list[str] = []
    for page in doc.get("pages", []):
        for table in page.get("tables", []):
            if table.get("kind") == "key_value":
                parts.extend((p.get("value") or "") for p in table.get("pairs", []))
            else:
                for row in table.get("rows", []) or []:
                    parts.extend(str(c) for c in row)
        for block in page.get("blocks", []):
            parts.append(block.get("text") or "")
    return _norm(" ".join(parts))


def _anchored(evidence: str, corpus: str) -> bool:
    n = _norm(evidence)
    return len(n) >= 4 and n in corpus


def _dedup_key(f: Fault) -> tuple[str, str, str]:
    return (_norm(f.title)[:60], _norm(f.section), _norm(f.table_ref))


def verify_and_tier(faults: list[Fault], doc: dict) -> list[Fault]:
    corpus = _doc_corpus(doc)
    kept: dict[tuple[str, str, str], Fault] = {}

    for f in faults:
        if f.evidence_class not in (EvidenceClass.CODE_VERIFIED, EvidenceClass.CHECKLIST):
            # sub-agent finding: anchor + tier by precedent
            if f.evidence and _anchored(f.evidence, corpus):
                f.evidence_class = EvidenceClass.QUOTE_ANCHORED
            else:
                f.evidence_class = EvidenceClass.MODEL_JUDGMENT
            if f.precedents:
                f.tier = Tier.CORROBORATED
                f.confidence = 0.6 if f.evidence_class == EvidenceClass.QUOTE_ANCHORED else 0.5
            else:
                f.tier = Tier.ADVISORY
                f.novel = True
                f.confidence = 0.4 if f.evidence_class == EvidenceClass.QUOTE_ANCHORED else 0.3

        key = _dedup_key(f)
        existing = kept.get(key)
        if existing is None:
            kept[key] = f
        elif _AUTHORITY[f.evidence_class] > _AUTHORITY[existing.evidence_class]:
            f.precedents = f.precedents or existing.precedents
            kept[key] = f
        elif not existing.precedents and f.precedents:
            existing.precedents = f.precedents  # merge precedent onto the survivor

    return sorted(
        kept.values(),
        key=lambda f: (_TIER_ORDER[f.tier], _SEV_ORDER[f.severity], -f.confidence),
    )
