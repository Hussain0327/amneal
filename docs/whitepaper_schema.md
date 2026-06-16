# White-Paper Field-Extraction Schema

**Status:** SHIPPED — this schema is implemented. The White Paper populator (PR #2, commit
`04f760e`; hardened in PR #3, `96ecd86`) builds the per-cell mapping below into
`src/regwatch/whitepaper/` (`populator.py`, `template.py`, `docx_writer.py`), backed by
migration `0005_whitepaper_sources`. This file remains the **field-extraction contract**: the
authoritative one-row-per-cell map of {source, endpoint/query or SPL section, lookup key, mode}
that the populator must honor. Edit this contract first when a cell's source or mode changes.
**Source template:** `CRA White Paper Template May 2026 - Raja.docx` (Arlin's per-field source
annotations preserved as Word comments in that file).
**Feature:** A populate-on-demand mode. **Input:** RLD name + NDA/application number.
**Output:** every template cell, each carrying provenance (source + record id/row + fetched
timestamp; page/section for PSG/SPL).

---

## How a cell gets populated (resolution spine)

The two inputs resolve a spine that every lookup keys off of:

```
RLD name + NDA #
   → appl_no                      (the application number)
   → Orange Book product_no set   (one NDA → one or more products)
   → ingredient / normalized_name (canonical active ingredient)
   → DailyMed setid               (current SPL for the labeling block)
```

## Mode legend

| Mode | Meaning |
|---|---|
| **auto** | Deterministic join keyed on appl_no / NDC / name. **No LLM.** The cell stores `source + record id/row + fetched_at`. |
| **evidence-only** | Value comes verbatim from a **cited** PSG RAG chunk or an extracted SPL LOINC section. Cited, **no generation**. |
| **manual** | Analyst-input-required judgment. The system **surfaces the underlying evidence** (patent rows, exclusivity, protected uses, PSG study list) and marks the cell `analyst_input_required`, reusing the `scope_warning` boundary. It **never** generates a value. |

### Absence-handling rule (applies to every `auto` presence / Yes–No cell)

A `No` / `not listed` value is valid **only** when the source was actually queried **and** the
record is genuinely absent. An inconclusive query (HTTP error, rate-limit, timeout) or a
low-confidence / ambiguous match must collapse the cell to `analyst_input_required` — **never**
emit a false negative. A wrong "No REMS" or "not on shortage" asserts an unverified fact and
violates INV-5 (verified provenance).

> ⚠ **REMS Y/N and Drug Shortages Y/N are the two highest-risk `auto` cells** — REMS is an HTML
> scrape with fuzzy application-number matching; Shortages is a live API that may rate-limit.
> Their handlers must distinguish **"queried, genuinely absent"** from **"query failed /
> ambiguous"** before ever rendering `No` (tri-state, not a bare boolean).

---

## Section 1 — Proposed Generic Product

| Cell | Source | Endpoint / Query / SPL Section | Lookup key | Mode |
|---|---|---|---|---|
| Product Name | Orange Book | `Products.txt` → `ingredient` | appl_no | auto |
| Dosage Form | Orange Book | `dosage_form_route` (split on `;`) | appl_no/product_no | auto |
| Route | Orange Book | `dosage_form_route` (split on `;`) | appl_no/product_no | auto |
| Strengths | Orange Book | `strength` (across the NDA's products) | appl_no | auto |
| R&D Center | **none — internal Amneal data** | — | — | manual |
| Priority Status | derived | OB patent + exclusivity rows | appl_no/product_no | manual |
| Patents: No Relevant / PI / PII / PIII / PIV / Section viii | Orange Book **`patent.txt`** | `patent_no`, `patent_use_code`, drug-substance/product flags | appl_no/product_no | manual *(surface rows; the paragraph classification is judgment)* |
| If PI — Eligible for First-to-Market? Y/N | derived | OB exclusivity + Paragraph IV list | appl_no | manual |
| If PIV — Eligible for eFTF? Y/N (PIV website review) | FDA Paragraph IV list + OB **`exclusivity.txt`** | PIV certifications page | appl_no | manual |
| Drug Shortages: On Shortage List? Y/N | Drug Shortages (DSS) | `shortages.json` `openfda.application_number:"NDA{appl}"` → `status` | appl_no | **auto ⚠** |
| Combination Product (Type 1–9) | derived — **NOT OB `type`** | Drugs@FDA products + 21 CFR 3.2(e) | appl_no | manual |
| Reference Listed Drug (RLD) | Orange Book | row where the `rld` flag = "Yes" (the live products.txt carries Yes/No) | appl_no | auto |
| Reference Standard (RS) | Orange Book | row where the `rs` flag = "Yes" (the live products.txt carries Yes/No) | appl_no/product_no | auto |

## Section 2 — Reference Listed Drug Product

| Cell | Source | Endpoint / Query / SPL Section | Lookup key | Mode |
|---|---|---|---|---|
| Proprietary Name | Orange Book / Drugs@FDA | `trade_name` / `brand_name` | appl_no | auto |
| RLD Strength | Orange Book | `strength` | appl_no/product_no | auto |
| NDA # | input / Drugs@FDA | `application_number` (confirm) | given | auto |
| NDA Holder | Drugs@FDA / Orange Book | `sponsor_name` / `applicant_full_name` | appl_no | auto |
| Indication | DailyMed SPL | LOINC **34067-9** (Indications & Usage) | setid | evidence-only |
| REMS | REMS | accessdata REMS index match on appl_no | appl_no | **auto ⚠** |
| Restricted Distribution | REMS / SPL | REMS ETASU rows | appl_no | manual *(interpretation of "restricted"; surface REMS row)* |
| Labeling Images | DailyMed | SPL `observationMedia` / `spls/{setid}/media.json` | setid | evidence-only *(enumerate/link assets only — no interpretation)* |
| Established Pharmacologic Class | DailyMed SPL / openFDA | EPC indexing / `openfda.pharm_class_epc` | setid / appl_no | evidence-only |
| PLR (Physician Labeling Rule) Format | DailyMed SPL structure | section-structure heuristic (Highlights + numbered sections) | setid | evidence-only *(low confidence — analyst override)* |
| USP Monograph (API Y/N, Finished Drug Product Y/N) | **none — USP-NF is paywalled** | — | — | manual |
| PLLR (Pregnancy & Lactation Labeling Rule) Format | DailyMed SPL | presence of LOINC 42228-7 / 77290-5 / 77291-3 subsections | setid | evidence-only |
| Pregnancy Registry Contact Detail | DailyMed SPL | text scan within the Pregnancy subsection | setid | evidence-only *(presence Y/N reliable; detail is extracted text)* |
| Salable Unit (from Sales & Marketing) | **none — internal (Sales & Marketing)** | — | — | manual |
| Packaging Configuration(s) | NDC / DailyMed | `ndc.json` `packaging[].package_ndc` (+ SPL packaging) | appl_no → product_ndc | auto |
| Labeling Carveouts | derived | OB `patent_use_code` ↔ RLD indications | appl_no | manual *(carve-out decision is judgment; surface use-codes + indications)* |
| Emergency Use | **none — EUA list is unstructured** | — | appl_no | manual |
| DEA Classification (I/II/III/IV/N-A) | DailyMed SPL / openFDA | `openfda.dea_schedule` / controlled-substance field | setid / appl_no | evidence-only |

## Section 3 — Product Specific Bioequivalence Recommendation Guidance

| Cell | Source | Endpoint / Query | Lookup key | Mode |
|---|---|---|---|---|
| BE Guidance Available | PSG store | local presence test: `rld_or_rs_number` contains appl_no | appl_no | auto |
| Requirements | PSG RAG | scoped `grounded_qa.ask()` with `normalized_name` filter | ingredient | evidence-only |
| BE Strategy → Proposed Strategy | derived | PSG study fields as evidence | ingredient | manual *(a "proposed" strategy is a recommendation — `scope_warning`)* |
| Required Studies → In Vivo BE Studies | PSG | `be_requirement.study_type` / `study_design` + citation | ingredient | manual *(per-study decision; surface PSG field)* |
| Required Studies → Q1/Q2 Assessment | PSG | PSG text / `waiver_conditions` | ingredient | manual *(surface evidence)* |
| Required Studies → Tablet Scoring | PSG | PSG text | ingredient | manual *(surface evidence)* |
| Required Studies → Threshold Analysis | PSG | PSG text | ingredient | manual *(surface evidence)* |
| Required Studies → Human Factor Studies | PSG | PSG text | ingredient | manual *(surface evidence)* |
| Required Studies → Dissolution Testing | PSG | `be_requirement.dissolution` + citation | ingredient | manual *(surface PSG `dissolution` field)* |
| Required Studies → Biocompatibility Studies | PSG | PSG text | ingredient | manual *(surface evidence)* |
| Required Studies → Toxicology Studies | PSG | PSG text | ingredient | manual *(surface evidence)* |

## Section 4 — Action Items

| Cell | Source | Mode |
|---|---|---|
| Prepared By | **none — workflow/people** | manual |
| Reviewed By | **none — workflow/people** | manual |
| Labeling Approved By | **none — workflow/people** | manual |
| Approved By | **none — workflow/people** | manual |

---

## Build gaps & cells needing Arlin's confirmation

These are the open items the schema surfaces — please confirm with Arlin before Gate 2 build:

1. **Cells with no FDA source → `manual` by necessity.** R&D Center, Salable Unit, USP
   Monograph (USP-NF is paywalled; FDA does not publish monograph existence as structured
   data), Emergency Use (EUA), and all four Action Items have no machine-readable FDA source.
   Confirm these stay analyst-entered.
2. **Combination Product Type 1–9.** Arlin annotated "Orange Book", but OB's `type` column is
   marketing status (RX/OTC) — **not** the 21 CFR 3.2(e) combination type. This is a
   determination → `manual` (we surface dosage form / device constituent as evidence).
3. **Patents / Priority / First-to-Market / eFTF / Labeling Carveouts.** Annotated "Orange
   Book". OB supplies the *raw* patent and exclusivity rows (numbers, expiry, use-codes,
   exclusivity codes), but the *classification* (which paragraph, eligibility) is regulatory
   judgment → `manual` with the rows attached as evidence. *(Shipped: the Orange Book loader
   now parses `patent.txt` + `exclusivity.txt` from the same ZIP — see
   `src/regwatch/sources/orange_book.py` `patent_rows()` / `exclusivity_rows()` — surfacing
   these rows raw.)*
4. **DailyMed is a new source.** Arlin annotated "DailyMed" / "Drugs@FDA, DailyMed" for the
   labeling block. *(Shipped: `src/regwatch/sources/dailymed.py` resolves `appl_no → setid` via
   DailyMed REST v2 and extracts SPL LOINC sections, replacing the prior openFDA `label.json`
   Indications + Dosage pull.)*
5. **PLR-format detection is heuristic.** "PLR format Y/N" is a formatting determination, not a
   discrete SPL field — marked `evidence-only` with a stated confidence note and analyst
   override, never a bare auto Y/N.

## Compliance boundary (why some annotated cells are `manual`)

- **INV-3 (no regulatory judgment):** patent paragraph classification, First-to-Market, eFTF,
  Priority Status, Combination Product type, Labeling Carveouts, Proposed BE Strategy, and
  every study-by-study Required-Studies decision are analyst judgments. The system surfaces
  evidence; it does not decide.
- **INV-5 (verified provenance):** cells with no verified FDA source are hard-empty + `manual`
  (never filled from model memory). A false negative on an `auto` presence cell is also an
  INV-5 violation → governed by the absence-handling rule above.

---

*Implemented:* the populator (`src/regwatch/whitepaper/` module + DailyMed source + Orange Book
patent/exclusivity parsing + persistence/caching with `last_fetched_at` freshness via migration
`0005_whitepaper_sources` + structured citations + `.docx` export) shipped in PR #2
(`04f760e`) and was hardened in PR #3 (`96ecd86`). Remaining/forward work — gold-set white-paper
row expansion (16 → 30-50) and applying the persist-and-cite + freshness pattern to the
Ask/Assemble read paths — is tracked in `docs/ROADMAP.md`.
