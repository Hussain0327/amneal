"""Canonical deficiency domains, and normalization of the KB's messy free-text types.

A "domain" is what one Stage-3 specialist owns (it can emit many findings within it).
The KB's `deficiency_type` column is inconsistent free text ("Method/Valdn",
"container/Closure", casing splits); `normalize_type` maps it onto these canonical names
so precedent retrieval and grouping don't silently miss.
"""

from __future__ import annotations

# Canonical domain -> what it covers. The description reaches the selector LLM.
CANONICAL_DOMAINS: dict[str, str] = {
    "specification": "Incomplete specifications, missing acceptance criteria, CoA discrepancies, limits not justified or not tightened to observed data.",
    "method-validation": "Analytical method not validated; missing ICH Q2 parameters (accuracy, precision, specificity, LOD/LOQ, linearity, robustness); inadequate system suitability.",
    "impurities": "Missing impurity limits, unqualified or unidentified impurities, ICH Q3A/Q3B thresholds not applied.",
    "elemental-impurities": "ICH Q3D risk-assessment gaps: wrong route/PDE basis, missing elements or classes, component (Option 2b) calculation errors.",
    "residual-solvents": "ICH Q3C solvents uncontrolled or skip-tested without justification; solvent-class label inconsistencies.",
    "nitrosamines": "Missing or inadequate nitrosamine risk assessment / acceptable-intake control where applicable.",
    "stability": "Insufficient duration or timepoints, out-of-trend results, unsupported shelf-life extrapolation (ICH Q1E).",
    "dissolution": "Missing or non-discriminatory dissolution/IVRT method; acceptance criteria not per product-specific guidance.",
    "container-closure": "Inadequate container-closure system; missing extractables/leachables (E&L) data; USP <87>/<88>/<661> coverage gaps; RLD-comparison and AET issues.",
    "manufacturing-process": "Process-validation gaps, inadequate in-process controls, process parameters inconsistent across sections.",
    "development": "Incomplete formulation or QbD justification; design-space ranges not supported by study data; excipient functionality gaps.",
    "batch-data": "Missing batch records, reconciliation issues, supporting-study batches not shown equivalent to the bio/exhibit batch.",
    "reference-standards": "Missing reference-standard characterization or traceability to a compendial/primary standard.",
    "commitments": "Forward-looking commitments left open -- ongoing monitoring, post-approval reporting, follow-up studies.",
    "coverage-gap": "An analysis applied to some members of a set but silently omitted for others, with no stated justification.",
}

# Raw KB deficiency_type (casefolded, trimmed) -> canonical domain.
_ALIASES: dict[str, str] = {
    "specification/coa": "specification",
    "specification": "specification",
    "description/compostion": "specification",
    "method/valdn": "method-validation",
    "method/validation": "method-validation",
    "impurities": "impurities",
    "elemental impurities": "elemental-impurities",
    "stability": "stability",
    "dissolution": "dissolution",
    "in-vitro study": "dissolution",
    "be": "dissolution",
    "container/closure": "container-closure",
    "extractable / leachable": "container-closure",
    "leachable": "container-closure",
    "manufacturing process/ scale-up": "manufacturing-process",
    "in-process controls": "manufacturing-process",
    "administrative/ facilities": "manufacturing-process",
    "pdr-qbd (development report)": "development",
    "characterization/justification": "development",
    "excipients": "development",
    "anda batch/ reconciliation / 32r": "batch-data",
}


def normalize_type(raw: str) -> str:
    """Map a raw KB deficiency_type to a canonical domain name, or '' if unrecognized."""
    if not raw:
        return ""
    return _ALIASES.get(str(raw).strip().casefold(), "")


def domain_catalog_text() -> str:
    return "\n".join(f"- {name}: {desc}" for name, desc in CANONICAL_DOMAINS.items())
