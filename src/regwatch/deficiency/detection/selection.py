"""Stage 3, part 1 -- pick the domains to deep-dive, and pull precedent for each.

The count is adaptive: the selector ranks the domains relevant to THIS document (up to a
cap). Selection only *adds* specialists -- the open reviewers still read every section, so a
domain that isn't selected is still covered by breadth. Never a fault ceiling.
"""

from __future__ import annotations

import json
import re

from regwatch.common.logging import get_logger
from regwatch.deficiency.detection.catalog import CANONICAL_DOMAINS, domain_catalog_text
from regwatch.deficiency.detection.prompts import DOMAIN_SELECTOR
from regwatch.deficiency.precedents import find_similar_deficiencies
from regwatch.deficiency.schemas.flaws import SimilarDeficiency
from regwatch.deficiency.structured import chat_completion
from regwatch.generate.llm import D1ResidencyError

log = get_logger(__name__)

_MAX_DOMAINS = 8

_FALLBACK_HINTS: dict[str, list[str]] = {
    "method-validation": [
        "validation",
        "linearity",
        "precision",
        "specificity",
        "system suitability",
        "lod",
        "loq",
    ],
    "impurities": ["impurit", "related compound", "related substance"],
    "elemental-impurities": ["elemental", "q3d", "metal"],
    "residual-solvents": ["residual solvent", "q3c"],
    "stability": ["stability"],
    "dissolution": ["dissolution", "ivrt", "in-vitro"],
    "container-closure": ["leachable", "extractable", "container", "closure", "e&l", "e & l"],
    "specification": ["specification", "acceptance criteria", "assay", "coa"],
}


def _doc_digest(sections: list[dict]) -> str:
    heads = [s.get("heading", "") for s in sections if s.get("heading")]
    return "Section headings:\n" + "\n".join(f"- {h}" for h in heads[:40])


def _parse_domain_array(resp: str) -> list[str]:
    match = re.search(r"\[.*\]", resp or "", re.S)
    if not match:
        return []
    try:
        arr = json.loads(match.group())
    except Exception:
        return []
    return [str(x).strip() for x in arr if isinstance(x, str)]


def _fallback_domains(sections: list[dict]) -> list[str]:
    text = " ".join(s.get("heading", "") + " " + s.get("text", "") for s in sections).lower()
    hits = [dom for dom, kws in _FALLBACK_HINTS.items() if any(k in text for k in kws)]
    return hits or ["specification", "method-validation", "impurities"]


def select_domains(doc: dict, sections: list[dict]) -> list[str]:
    picked: list[str] = []
    try:
        resp = chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": DOMAIN_SELECTOR.format(catalog=domain_catalog_text()),
                },
                {
                    "role": "user",
                    "content": f"Document: {doc.get('filename', '')}\n\n{_doc_digest(sections)}",
                },
            ],
            max_tokens=200,
        )
        picked = [d for d in _parse_domain_array(resp) if d in CANONICAL_DOMAINS]
    except D1ResidencyError:
        # A residency violation must fail the run loudly, not degrade into keyword fallback.
        raise
    except Exception as exc:
        log.warning("domain_selection_failed", error=str(exc)[:200])

    if not picked:
        picked = _fallback_domains(sections)

    seen: set[str] = set()
    ordered: list[str] = []
    for d in picked:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered[:_MAX_DOMAINS]


def gather_precedents(domain: str, doc: dict, top_k: int = 3) -> list[SimilarDeficiency]:
    query = f"{domain}: {CANONICAL_DOMAINS.get(domain, '')} -- {doc.get('filename', '')}"
    try:
        return find_similar_deficiencies(query, top_k=top_k)
    except Exception as exc:  # retrieval is best-effort; absence of precedent is not an error
        log.warning("precedent_retrieval_failed", domain=domain, error=str(exc)[:200])
        return []
