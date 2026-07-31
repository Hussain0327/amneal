"""The fault-detection layer's output types.

A `Fault` is one candidate deficiency surfaced to the analyst. It carries not just the
claim and its evidence but *how far we could stand behind it*: the `tier` (how confident),
the `evidence_class` (what kind of check backed it), and the precedent it matched. The
design rule is recall-biased -- a Fault is only ever *downgraded*, never silently dropped,
except when a deterministic oracle proves it false (that Fault is filtered before this
report is built).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from regwatch.deficiency.schemas.flaws import FlawCategory, Severity, SimilarDeficiency
from regwatch.deficiency.schemas.llm import ParseFailed


class EvidenceClass(StrEnum):
    """What kind of check stands behind the finding -- surfaced so the analyst never
    mistakes a model opinion for a code-verified fact."""

    CODE_VERIFIED = "code_verified"  # an oracle recomputed / compared cells
    CHECKLIST = "checklist"  # a required element was searched for and is absent
    QUOTE_ANCHORED = "quote_anchored"  # the cited evidence span exists verbatim in the doc
    MODEL_JUDGMENT = "model_judgment"  # LLM reasoning only, no oracle or anchor


class Tier(StrEnum):
    """Confidence tier. Recall lives in ADVISORY -- nothing is hidden, only ranked."""

    VERIFIED = "verified"  # T1 -- oracle-confirmed, or strong precedent + self-consistency
    CORROBORATED = "corroborated"  # T2 -- >=1 real precedent, no hard oracle
    ADVISORY = "advisory"  # T3 -- model judgment, incl. novel / out-of-distribution


class Fault(BaseModel):
    """One candidate deficiency."""

    title: str = Field(description="One-line statement of the deficiency.")
    detail: str = Field(
        default="", description="What is wrong and why it matters, argued from the evidence."
    )
    category: FlawCategory = FlawCategory.GENERAL_CMC
    severity: Severity = Severity.MEDIUM

    tier: Tier = Tier.ADVISORY
    evidence_class: EvidenceClass = EvidenceClass.MODEL_JUDGMENT
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    evidence: str = Field(default="", description="Verbatim span or cell the finding rests on.")
    section: str = Field(default="", description="Section heading the fault sits in.")
    page: int = 0
    table_ref: str = Field(default="", description="Table the fault concerns, e.g. 'Table 16'.")

    source: str = Field(
        default="",
        description=(
            "What produced it, e.g. 'oracle:result_vs_limit', "
            "'specialist:elemental_impurities', 'reviewer:1.4.6'."
        ),
    )
    guidance_refs: list[str] = Field(default_factory=list)
    precedents: list[SimilarDeficiency] = Field(default_factory=list)

    novel: bool = Field(default=False, description="No matching precedent in the KB.")
    out_of_distribution: bool = Field(
        default=False, description="Doc type the KB does not cover well (e.g. non-patch)."
    )
    challenge_note: str = Field(
        default="",
        description="Grounded counter-evidence found by the challenge pass, if it lowered confidence.",
    )


class FaultReport(BaseModel):
    """The detection layer's output -> frontend."""

    job_id: str = ""
    faults: list[Fault] = Field(default_factory=list)
    faults_found: bool = False
    domains_checked: list[str] = Field(default_factory=list)
    parse_failures: list[ParseFailed] = Field(default_factory=list)
    analysis_seconds: float = 0.0
