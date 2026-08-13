# White-Paper Field-Extraction Schema

**Status: shipped.** Last updated: 2026-08-13 for the FDA-only source policy.

This file is the field-extraction contract: one row per template cell, giving
the source, the endpoint or label section, the lookup key, and the mode. Edit this
contract first when a cell's source or mode changes.

The code mirrors it cell for cell. `src/regwatch/whitepaper/template.py` encodes
the 46 cells below as an ordered `CellSpec` registry (verified 2026-08-11: 46
specs), `populator.py` fills them, `docx_writer.py` renders them, and
`tests/test_whitepaper_invariants.py` walks the registry to prove no manual cell
ever carries a generated value. Persistence is migrations `0005_whitepaper_sources`
(source snapshots), `0006_ob_appl_type` (Orange Book application type) and
`0013_whitepaper_runs` (runs plus the analyst overlay).

Source template: `CRA White Paper Template May 2026 - Raja.docx`. Arlin's
per-field source annotations are preserved as Word comments in that file.

Feature: populate on demand. Input: RLD name plus NDA or application number.
Output: every template cell, each carrying provenance (source, record id or row,
fetched timestamp, plus page or section for PSG and approved labeling).

---

## How a cell gets populated

The two inputs resolve a spine that every lookup keys off:

```
RLD name + NDA #
   -> appl_no                      (the application number)
   -> Orange Book product_no set   (one NDA maps to one or more products)
   -> ingredient / normalized_name (canonical active ingredient)
   -> Drugs@FDA approved label ID  (current indexed labeling document)
```

## Mode legend

The code calls these `CellMode.AUTO`, `CellMode.EVIDENCE_ONLY` and
`CellMode.MANUAL`.

| Mode | Meaning |
|---|---|
| **auto** | Deterministic join on application number, product number, or normalized name. No LLM. The cell stores source, record ID or row, and `fetched_at`. |
| **evidence_only** | The value comes verbatim from a cited FDA chunk or an extracted approved-label section. Cited, never generated. |
| **manual** | Analyst judgment. The system surfaces the underlying evidence (patent rows, exclusivity, protected uses, PSG study list) and marks the cell `analyst_input_required`. It never generates a value. |

### Absence rule, for every auto presence or Yes/No cell

A "No" or "not listed" value is valid only when the source was actually queried
and the record is genuinely absent. A query that did not conclude (HTTP error,
rate limit, timeout) or a low-confidence or ambiguous match collapses the cell to
`analyst_input_required`. Never emit a false negative. Fields whose former
source is outside the five-family corpus, including shortage and REMS status,
now remain analyst input; they cannot render a corpus-backed "No".

---

## Section 1: Proposed Generic Product

| Cell | Source | Endpoint / Query / SPL Section | Lookup key | Mode |
|---|---|---|---|---|
| Product Name | Orange Book | `products.txt` -> `ingredient` | appl_no | auto |
| Dosage Form | Orange Book | `dosage_form_route`, split on `;` | appl_no/product_no | auto |
| Route | Orange Book | `dosage_form_route`, split on `;` | appl_no/product_no | auto |
| Strengths | Orange Book | `strength` across the application's products | appl_no | auto |
| R&D Center | none, internal Amneal data | - | - | manual |
| Priority Status | derived | OB patent + exclusivity rows | appl_no/product_no | manual |
| Patents: No Relevant / PI / PII / PIII / PIV / Section viii | Orange Book `patent.txt` | `patent_no`, `patent_use_code`, drug-substance/product flags | appl_no/product_no | manual, surface the rows; the paragraph classification is judgment |
| If PI, eligible for First-to-Market? Y/N | derived | OB exclusivity + Paragraph IV list | appl_no | manual |
| If PIV, eligible for eFTF? Y/N (PIV website review) | FDA Paragraph IV list + OB `exclusivity.txt` | PIV certifications page | appl_no | manual |
| Drug Shortages: on shortage list? Y/N | none in approved corpus | retired source; no automated lookup | appl_no | analyst_input_required |
| Combination Product (Type 1-9) | derived, NOT the OB `type` column | Drugs@FDA products + 21 CFR 3.2(e) | appl_no | manual |
| Reference Listed Drug (RLD) | Orange Book | row where the `rld` flag is "Yes" | appl_no | auto |
| Reference Standard (RS) | Orange Book | row where the `rs` flag is "Yes" | appl_no/product_no | auto |

## Section 2: Reference Listed Drug Product

| Cell | Source | Endpoint / Query / SPL Section | Lookup key | Mode |
|---|---|---|---|---|
| Proprietary Name | Orange Book / Drugs@FDA | `trade_name` / `brand_name` | appl_no | auto |
| RLD Strength | Orange Book | `strength` | appl_no/product_no | auto |
| NDA # | input / Drugs@FDA | `application_number`, confirmed | given | auto |
| NDA Holder | Drugs@FDA / Orange Book | `sponsor_name` / `applicant_full_name` | appl_no | auto |
| Indication | Drugs@FDA approved labeling | Indications and Usage section | appl_no / page | evidence_only |
| REMS | none in approved corpus | retired source; no automated lookup | appl_no | analyst_input_required |
| Restricted Distribution | none in approved corpus | analyst determination | appl_no | manual |
| Labeling Images | Drugs@FDA approved labeling | approved-label PDF pages | appl_no | evidence_only; document surfaced, image enumeration requires analyst review |
| Established Pharmacologic Class | Drugs@FDA approved labeling | approved-label text / EPC indexing | appl_no / page | evidence_only |
| PLR (Physician Labeling Rule) Format | Drugs@FDA approved-label structure | Highlights + numbered-section heuristic | appl_no / page | evidence_only, low confidence, analyst can override |
| USP Monograph (API Y/N, Finished Drug Product Y/N) | none, USP-NF is paywalled | - | - | manual |
| PLLR (Pregnancy and Lactation Labeling Rule) Format | Drugs@FDA approved labeling | Pregnancy, Lactation and Reproductive Potential subsection presence | appl_no / page | evidence_only |
| Pregnancy Registry Contact Detail | Drugs@FDA approved labeling | text scan inside the Pregnancy subsection | appl_no / page | evidence_only; detail is extracted text |
| Salable Unit (from Sales and Marketing) | none, internal | - | - | manual |
| Packaging Configuration(s) | Drugs@FDA approved labeling / analyst input | approved-label packaging text | appl_no / page | evidence_only |
| Labeling Carveouts | derived | OB `patent_use_code` against RLD indications | appl_no | manual, the carve-out decision is judgment; surface use-codes and indications |
| Emergency Use | none, the EUA list is unstructured | - | appl_no | manual |
| DEA Classification (I/II/III/IV/N-A) | Drugs@FDA approved labeling / analyst input | no approved structured corpus field | appl_no | evidence_only; absence never proves N/A |

## Section 3: Product Specific Bioequivalence Recommendation Guidance

| Cell | Source | Endpoint / Query | Lookup key | Mode |
|---|---|---|---|---|
| BE Guidance Available | PSG store | local presence test: `rld_or_rs_number` contains appl_no | appl_no | auto |
| Requirements | PSG RAG | scoped `grounded_qa.ask()` with a `normalized_name` filter | ingredient | evidence_only |
| BE Strategy: Proposed Strategy | derived | PSG study fields as evidence | ingredient | manual, a proposed strategy is a recommendation |
| Required Studies: In Vivo BE Studies | PSG | `be_requirement.study_type` / `study_design` + citation | ingredient | manual, per-study decision; surface the PSG field |
| Required Studies: Q1/Q2 Assessment | PSG | PSG text / `waiver_conditions` | ingredient | manual, surface evidence |
| Required Studies: Tablet Scoring | PSG | PSG text | ingredient | manual, surface evidence |
| Required Studies: Threshold Analysis | PSG | PSG text | ingredient | manual, surface evidence |
| Required Studies: Human Factor Studies | PSG | PSG text | ingredient | manual, surface evidence |
| Required Studies: Dissolution Testing | PSG | `be_requirement.dissolution` + citation | ingredient | manual, surface the PSG `dissolution` field |
| Required Studies: Biocompatibility Studies | PSG | PSG text | ingredient | manual, surface evidence |
| Required Studies: Toxicology Studies | PSG | PSG text | ingredient | manual, surface evidence |

## Section 4: Action Items

| Cell | Source | Mode |
|---|---|---|
| Prepared By | none, workflow and people | manual |
| Reviewed By | none, workflow and people | manual |
| Labeling Approved By | none, workflow and people | manual |
| Approved By | none, workflow and people | manual |

---

## Cells that needed Arlin's confirmation

1. **Cells with no FDA source are manual by necessity.** R&D Center, Salable
   Unit, USP Monograph (USP-NF is paywalled and FDA does not publish monograph
   existence as structured data), Emergency Use, and all four Action Items have
   no machine-readable FDA source. Confirm these stay analyst-entered.
2. **Combination Product Type 1-9.** Annotated "Orange Book", but OB's `type`
   column is marketing status (RX or OTC), not the 21 CFR 3.2(e) combination
   type. That is a determination, so the cell is manual and we surface the
   dosage form and device constituent as evidence.
3. **Patents, Priority, First-to-Market, eFTF, Labeling Carveouts.** Annotated
   "Orange Book". OB gives the raw patent and exclusivity rows: numbers, expiry,
   use codes, exclusivity codes. The classification (which paragraph, whether
   eligible) is regulatory judgment, so these are manual with the rows attached
   as evidence. Shipped: the Orange Book loader parses `patent.txt` and
   `exclusivity.txt` from the same ZIP, see `sources/orange_book.py`
   `patent_rows()` and `exclusivity_rows()`.
4. **The labeling block now uses only approved Drugs@FDA labeling.** The corpus
   indexes FDA label documents by application and page. Legacy SPL fields remain
   nullable only so saved older runs deserialize; no external label lookup runs.
5. **PLR-format detection is a heuristic.** "PLR format Y/N" is a formatting
   determination, not a discrete SPL field, so it is `evidence_only` with a
   stated confidence note and an analyst override. Never a bare auto Y/N.

## Why some annotated cells are manual

- **INV-3, no regulatory judgment.** Patent paragraph classification,
  First-to-Market, eFTF, Priority Status, Combination Product type, Labeling
  Carveouts, Proposed BE Strategy, and every study-by-study Required Studies
  decision are analyst judgments. The system surfaces evidence; it does not
  decide.
- **INV-5, verified provenance.** A cell with no verified FDA source is left
  empty and marked manual, never filled from model memory. A false negative on
  an auto presence cell is also an INV-5 violation, which is what the absence
  rule above prevents.

---

*What shipped:* the populator module, indexed Drugs@FDA approved-label reading,
Orange Book patent and exclusivity parsing, persistence and caching with
`last_fetched_at` freshness, structured citations, and `.docx` export. Landed in PR #2
(`04f760e`), hardened in PR #3 (`96ecd86`). Still open: expanding the white-paper
gold set from its current 16 rows (`src/regwatch/eval/whitepaper_gold.jsonl`,
counted 2026-08-11) to 30-50, and applying the persist-and-cite plus freshness
pattern to the Ask and Assemble read paths. Both are tracked in
[`ROADMAP.md`](ROADMAP.md).
