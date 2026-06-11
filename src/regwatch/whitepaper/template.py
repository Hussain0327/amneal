"""The White-Paper schema encoded as an ordered ``CellSpec`` registry.

This module is the single source of truth that mirrors ``docs/whitepaper_schema.md``
cell by cell — every section, every label, every mode (including every manual
cell). The populator iterates this registry; the docx writer maps it onto the
Word template; the invariants test walks it to prove no manual cell ever carries
a generated value (INV-3/INV-5).

``extractor`` is the dispatch key into ``populator.EXTRACTORS``. ``arg`` carries
an optional extractor parameter (a LOINC code, a ``be_requirement`` field name,
etc.) so several cells can share one extractor without 1:1 function sprawl.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CellMode(StrEnum):
    """How a cell gets a value (see the schema's mode legend)."""

    AUTO = "auto"  # deterministic join, no LLM; stores source + locator + fetched_at
    EVIDENCE_ONLY = "evidence_only"  # verbatim cited PSG/SPL text, no generation
    MANUAL = "manual"  # analyst judgment; surface evidence, never emit a value


# Section titles — kept verbatim from the schema so the docx writer and the
# wire payload agree on grouping.
SECTION_1 = "Proposed Generic Product"
SECTION_2 = "Reference Listed Drug Product"
SECTION_3 = "Product Specific Bioequivalence Recommendation Guidance"
SECTION_4 = "Action Items"


@dataclass(frozen=True)
class CellSpec:
    """One template cell's source/lookup/mode, mirroring the schema row."""

    id: str
    section: str
    label: str
    source: str  # human-facing source label ("Orange Book", "DailyMed SPL", ...)
    lookup: str  # endpoint / SPL section / lookup key (mirrors the schema)
    mode: CellMode
    extractor: str  # dispatch key into populator.EXTRACTORS
    arg: str | None = None  # optional extractor parameter (LOINC / field name)


# Cells rendered as a Yes/No (or checkbox) line in the Word template. The docx
# writer APPENDS an explicit "-> Yes/No/Analyst input required" marker to these
# rather than overwriting the template's checkbox text.
CHECKBOX_CELL_IDS = frozenset(
    {
        "drug_shortage",
        "combination_product",
        "first_to_market",
        "eftf",
        "rems",
        "restricted_distribution",
        "plr_format",
        "usp_monograph",
        "pllr_format",
        "pregnancy_registry",
        "emergency_use",
        "dea_classification",
    }
)


# ---------------------------------------------------------------------------
# The registry — ordered exactly as the schema / template present the cells.
# ---------------------------------------------------------------------------
CELL_SPECS: tuple[CellSpec, ...] = (
    # ----- Section 1 — Proposed Generic Product -----
    CellSpec(
        "product_name",
        SECTION_1,
        "Product Name",
        "Orange Book",
        "products.txt -> ingredient (appl_no)",
        CellMode.AUTO,
        "ob_product_name",
    ),
    CellSpec(
        "dosage_form",
        SECTION_1,
        "Dosage Form",
        "Orange Book",
        "dosage_form_route split on ';' (appl_no/product_no)",
        CellMode.AUTO,
        "ob_dosage_form",
    ),
    CellSpec(
        "route",
        SECTION_1,
        "Route",
        "Orange Book",
        "dosage_form_route split on ';' (appl_no/product_no)",
        CellMode.AUTO,
        "ob_route",
    ),
    CellSpec(
        "strengths",
        SECTION_1,
        "Strengths",
        "Orange Book",
        "strength across the application's products (appl_no)",
        CellMode.AUTO,
        "ob_strengths",
    ),
    CellSpec(
        "rd_center",
        SECTION_1,
        "R&D Center",
        "none — internal Amneal data",
        "—",
        CellMode.MANUAL,
        "manual_no_source",
    ),
    CellSpec(
        "priority_status",
        SECTION_1,
        "Priority Status",
        "derived",
        "OB patent + exclusivity rows (appl_no/product_no)",
        CellMode.MANUAL,
        "priority_block",
    ),
    CellSpec(
        "patents",
        SECTION_1,
        "Patents (No Relevant / PI / PII / PIII / PIV / Section viii)",
        "Orange Book patent.txt",
        "patent_no, patent_use_code, drug-substance/product flags (appl_no/product_no)",
        CellMode.MANUAL,
        "patent_block",
    ),
    CellSpec(
        "first_to_market",
        SECTION_1,
        "If PI — Eligible for First-to-Market? Y/N",
        "derived",
        "OB exclusivity + Paragraph IV list (appl_no)",
        CellMode.MANUAL,
        "exclusivity_block",
    ),
    CellSpec(
        "eftf",
        SECTION_1,
        "If PIV — Eligible for eFTF? Y/N (PIV website review)",
        "FDA Paragraph IV list + OB exclusivity.txt",
        "PIV certifications page (appl_no)",
        CellMode.MANUAL,
        "exclusivity_block",
    ),
    CellSpec(
        "drug_shortage",
        SECTION_1,
        "Drug Shortages: On Shortage List? Y/N",
        "Drug Shortages (DSS)",
        "shortages.json openfda.application_number -> status (appl_no)",
        CellMode.AUTO,
        "shortage",
    ),
    CellSpec(
        "combination_product",
        SECTION_1,
        "Combination Product (Type 1-9)",
        "derived — NOT OB type",
        "Drugs@FDA products + 21 CFR 3.2(e) (appl_no)",
        CellMode.MANUAL,
        "combination_product",
    ),
    CellSpec(
        "rld",
        SECTION_1,
        "Reference Listed Drug (RLD)",
        "Orange Book",
        "row where the rld flag = 'Yes' (appl_no)",
        CellMode.AUTO,
        "ob_rld",
    ),
    CellSpec(
        "rs",
        SECTION_1,
        "Reference Standard (RS)",
        "Orange Book",
        "row where the rs flag = 'Yes' (appl_no/product_no)",
        CellMode.AUTO,
        "ob_rs",
    ),
    # ----- Section 2 — Reference Listed Drug Product -----
    CellSpec(
        "proprietary_name",
        SECTION_2,
        "Proprietary Name",
        "Orange Book / Drugs@FDA",
        "trade_name / brand_name (appl_no)",
        CellMode.AUTO,
        "ob_proprietary_name",
    ),
    CellSpec(
        "rld_strength",
        SECTION_2,
        "RLD Strength",
        "Orange Book",
        "strength (appl_no/product_no)",
        CellMode.AUTO,
        "ob_strengths",
    ),
    CellSpec(
        "nda_number",
        SECTION_2,
        "NDA #",
        "input / Drugs@FDA",
        "application_number (confirm)",
        CellMode.AUTO,
        "nda_number",
    ),
    CellSpec(
        "nda_holder",
        SECTION_2,
        "NDA Holder",
        "Drugs@FDA / Orange Book",
        "sponsor_name / applicant_full_name (appl_no)",
        CellMode.AUTO,
        "nda_holder",
    ),
    CellSpec(
        "indication",
        SECTION_2,
        "Indication",
        "DailyMed SPL",
        "LOINC 34067-9 (Indications & Usage) (setid)",
        CellMode.EVIDENCE_ONLY,
        "spl_section",
        "34067-9",
    ),
    CellSpec(
        "rems",
        SECTION_2,
        "REMS",
        "REMS",
        "accessdata REMS index match on appl_no",
        CellMode.AUTO,
        "rems",
    ),
    CellSpec(
        "restricted_distribution",
        SECTION_2,
        "Restricted Distribution",
        "REMS / SPL",
        "REMS ETASU rows (appl_no)",
        CellMode.MANUAL,
        "restricted_distribution",
    ),
    CellSpec(
        "labeling_images",
        SECTION_2,
        "Labeling Images",
        "DailyMed",
        "spls/{setid}/media.json (setid)",
        CellMode.EVIDENCE_ONLY,
        "spl_media",
    ),
    CellSpec(
        "epc",
        SECTION_2,
        "Established Pharmacologic Class",
        "DailyMed SPL / openFDA",
        "pharm_class_epc / EPC indexing (setid/appl_no)",
        CellMode.EVIDENCE_ONLY,
        "epc",
    ),
    CellSpec(
        "plr_format",
        SECTION_2,
        "PLR (Physician Labeling Rule) Format",
        "DailyMed SPL structure",
        "Highlights + numbered-section heuristic (setid)",
        CellMode.EVIDENCE_ONLY,
        "plr_format",
    ),
    CellSpec(
        "usp_monograph",
        SECTION_2,
        "USP Monograph (API Y/N, Finished Drug Product Y/N)",
        "none — USP-NF is paywalled",
        "—",
        CellMode.MANUAL,
        "manual_no_source",
    ),
    CellSpec(
        "pllr_format",
        SECTION_2,
        "PLLR (Pregnancy & Lactation Labeling Rule) Format",
        "DailyMed SPL",
        "presence of LOINC 42228-7 / 77290-5 / 77291-3 (setid)",
        CellMode.EVIDENCE_ONLY,
        "pllr_format",
    ),
    CellSpec(
        "pregnancy_registry",
        SECTION_2,
        "Pregnancy Registry Contact Detail",
        "DailyMed SPL",
        "text scan within the Pregnancy subsection 42228-7 (setid)",
        CellMode.EVIDENCE_ONLY,
        "pregnancy_registry",
    ),
    CellSpec(
        "salable_unit",
        SECTION_2,
        "Salable Unit (from Sales & Marketing)",
        "none — internal (Sales & Marketing)",
        "—",
        CellMode.MANUAL,
        "manual_no_source",
    ),
    CellSpec(
        "packaging",
        SECTION_2,
        "Packaging Configuration(s)",
        "NDC / DailyMed",
        "ndc.json packaging[].package_ndc (appl_no -> product_ndc)",
        CellMode.AUTO,
        "packaging",
    ),
    CellSpec(
        "labeling_carveouts",
        SECTION_2,
        "Labeling Carveouts",
        "derived",
        "OB patent_use_code <-> RLD indications (appl_no)",
        CellMode.MANUAL,
        "labeling_carveouts",
    ),
    CellSpec(
        "emergency_use",
        SECTION_2,
        "Emergency Use",
        "none — EUA list is unstructured",
        "—",
        CellMode.MANUAL,
        "manual_no_source",
    ),
    CellSpec(
        "dea_classification",
        SECTION_2,
        "DEA Classification (I/II/III/IV/N-A)",
        "DailyMed SPL / openFDA",
        "openfda.dea_schedule / controlled-substance field",
        CellMode.EVIDENCE_ONLY,
        "dea",
    ),
    # ----- Section 3 — Product Specific Bioequivalence Recommendation Guidance -----
    CellSpec(
        "be_guidance_available",
        SECTION_3,
        "BE Guidance Available",
        "PSG store",
        "local presence test: rld_or_rs_number contains appl_no",
        CellMode.AUTO,
        "be_guidance_available",
    ),
    CellSpec(
        "requirements",
        SECTION_3,
        "Requirements",
        "PSG RAG",
        "scoped grounded_qa.ask() with normalized_name filter (ingredient)",
        CellMode.EVIDENCE_ONLY,
        "psg_requirements",
    ),
    CellSpec(
        "proposed_strategy",
        SECTION_3,
        "BE Strategy → Proposed Strategy",
        "derived",
        "PSG study fields as evidence (ingredient)",
        CellMode.MANUAL,
        "psg_strategy",
    ),
    CellSpec(
        "in_vivo_be_studies",
        SECTION_3,
        "Required Studies → In Vivo BE Studies",
        "PSG",
        "be_requirement.study_type / study_design + citation (ingredient)",
        CellMode.MANUAL,
        "be_requirement_field",
        "study_type",
    ),
    CellSpec(
        "q1_q2_assessment",
        SECTION_3,
        "Required Studies → Q1/Q2 Assessment",
        "PSG",
        "PSG text / waiver_conditions (ingredient)",
        CellMode.MANUAL,
        "be_requirement_field",
        "waiver_conditions",
    ),
    CellSpec(
        "tablet_scoring",
        SECTION_3,
        "Required Studies → Tablet Scoring",
        "PSG",
        "PSG text (ingredient)",
        CellMode.MANUAL,
        "be_requirement_field",
    ),
    CellSpec(
        "threshold_analysis",
        SECTION_3,
        "Required Studies → Threshold Analysis",
        "PSG",
        "PSG text (ingredient)",
        CellMode.MANUAL,
        "be_requirement_field",
    ),
    CellSpec(
        "human_factor_studies",
        SECTION_3,
        "Required Studies → Human Factor Studies",
        "PSG",
        "PSG text (ingredient)",
        CellMode.MANUAL,
        "be_requirement_field",
    ),
    CellSpec(
        "dissolution_testing",
        SECTION_3,
        "Required Studies → Dissolution Testing",
        "PSG",
        "be_requirement.dissolution + citation (ingredient)",
        CellMode.MANUAL,
        "be_requirement_field",
        "dissolution",
    ),
    CellSpec(
        "biocompatibility_studies",
        SECTION_3,
        "Required Studies → Biocompatibility Studies",
        "PSG",
        "PSG text (ingredient)",
        CellMode.MANUAL,
        "be_requirement_field",
    ),
    CellSpec(
        "toxicology_studies",
        SECTION_3,
        "Required Studies → Toxicology Studies",
        "PSG",
        "PSG text (ingredient)",
        CellMode.MANUAL,
        "be_requirement_field",
    ),
    # ----- Section 4 — Action Items -----
    CellSpec(
        "prepared_by",
        SECTION_4,
        "Prepared By",
        "none — workflow/people",
        "—",
        CellMode.MANUAL,
        "manual_no_source",
    ),
    CellSpec(
        "reviewed_by",
        SECTION_4,
        "Reviewed By",
        "none — workflow/people",
        "—",
        CellMode.MANUAL,
        "manual_no_source",
    ),
    CellSpec(
        "labeling_approved_by",
        SECTION_4,
        "Labeling Approved By",
        "none — workflow/people",
        "—",
        CellMode.MANUAL,
        "manual_no_source",
    ),
    CellSpec(
        "approved_by",
        SECTION_4,
        "Approved By",
        "none — workflow/people",
        "—",
        CellMode.MANUAL,
        "manual_no_source",
    ),
)


def section_order() -> list[str]:
    """The section titles in template order (de-duplicated)."""
    out: list[str] = []
    for spec in CELL_SPECS:
        if spec.section not in out:
            out.append(spec.section)
    return out


def specs_for_section(section: str) -> list[CellSpec]:
    return [spec for spec in CELL_SPECS if spec.section == section]


def spec_by_id(cell_id: str) -> CellSpec | None:
    return next((spec for spec in CELL_SPECS if spec.id == cell_id), None)
