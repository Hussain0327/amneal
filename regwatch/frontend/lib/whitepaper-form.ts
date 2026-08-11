// The printed CRA form, as the screen has to draw it.
//
// The wire sends FOUR sections; the Word template prints SEVEN tables -- it
// splits labeling out of the RLD section, and "BE Strategy" / "Required
// Studies" out of the PSG section. Mirroring the printed grouping here is what
// makes the page on screen and the .docx that downloads the same document.
//
// `label` here is the TEMPLATE's printed label. It is used for exactly one
// thing: drawing the blank form before any run exists. A loaded run always
// renders the API's own label for a cell, so the wire wins wherever the two
// ever drift, and a cell the template does not know about is never dropped --
// it falls back to its wire section (see groupCells).

import type { WhitepaperCell, WhitepaperEvidence, WhitepaperSectionData } from "@/lib/api";

export interface FormTableSpec {
  title: string;
  cells: readonly { readonly id: string; readonly label: string }[];
}

export const FORM_TABLES: readonly FormTableSpec[] = [
  {
    title: "Proposed Generic Product",
    cells: [
      { id: "product_name", label: "Product Name" },
      { id: "dosage_form", label: "Dosage Form" },
      { id: "route", label: "Route" },
      { id: "strengths", label: "Strengths" },
      { id: "rd_center", label: "R&D Center" },
      { id: "priority_status", label: "Priority Status" },
      { id: "patents", label: "Patents (OB review)" },
      { id: "first_to_market", label: "If PI - Eligible for First-to-Market?" },
      { id: "eftf", label: "If PIV - Eligible for eFTF?" },
      { id: "drug_shortage", label: "On Drug Shortage List?" },
      { id: "combination_product", label: "Combination Product" },
      { id: "rld", label: "Reference Listed Drug (RLD)" },
      { id: "rs", label: "Reference Standard (RS)" },
    ],
  },
  {
    title: "Reference Listed Drug Product",
    cells: [
      { id: "proprietary_name", label: "Proprietary Name" },
      { id: "rld_strength", label: "RLD Strength" },
      { id: "nda_number", label: "NDA #" },
      { id: "nda_holder", label: "NDA Holder" },
      { id: "indication", label: "Indication" },
      { id: "rems", label: "REMS" },
      { id: "restricted_distribution", label: "Restricted Distribution" },
      { id: "labeling_images", label: "Labeling Images" },
    ],
  },
  {
    title: "Labeling",
    cells: [
      { id: "epc", label: "Established Pharmacologic Class" },
      { id: "plr_format", label: "PLR (Physician Labeling Rule) Format" },
      { id: "usp_monograph", label: "USP Monograph" },
      { id: "pllr_format", label: "PLLR (Pregnancy and Lactation Labeling Rule) Format" },
      { id: "pregnancy_registry", label: "Pregnancy Registry Contact Detail" },
      { id: "salable_unit", label: "Salable Unit (from Sales and Marketing)" },
      { id: "packaging", label: "Packaging Configuration(s)" },
      { id: "labeling_carveouts", label: "Labeling Carveouts" },
      { id: "emergency_use", label: "Emergency Use" },
      { id: "dea_classification", label: "DEA Classification" },
    ],
  },
  {
    title: "Product Specific Bioequivalence Recommendation Guidance",
    cells: [
      { id: "be_guidance_available", label: "BE Guidance Available" },
      { id: "requirements", label: "Requirements" },
    ],
  },
  {
    title: "BE Strategy",
    cells: [{ id: "proposed_strategy", label: "Proposed Strategy" }],
  },
  {
    title: "Required Studies",
    cells: [
      { id: "in_vivo_be_studies", label: "In Vivo BE Studies" },
      { id: "q1_q2_assessment", label: "Qualitatively (Q1) and Quantitatively (Q2) Assessment" },
      { id: "tablet_scoring", label: "Tablet Scoring" },
      { id: "threshold_analysis", label: "Threshold Analysis" },
      { id: "human_factor_studies", label: "Human Factor Studies" },
      { id: "dissolution_testing", label: "Dissolution Testing" },
      { id: "biocompatibility_studies", label: "Biocompatibility Studies" },
      { id: "toxicology_studies", label: "Toxicology Studies" },
    ],
  },
  {
    title: "Action Items",
    cells: [
      { id: "prepared_by", label: "Prepared By" },
      { id: "reviewed_by", label: "Reviewed By" },
      { id: "labeling_approved_by", label: "Labeling Approved By" },
      { id: "approved_by", label: "Approved By" },
    ],
  },
];

export interface FormRow {
  cell: WhitepaperCell;
  /** The label as printed in this table (group prefix stripped). */
  label: string;
}

export interface FormGroup {
  title: string;
  rows: FormRow[];
}

// The template suffixes a group's own name onto its cell labels ("Required
// Studies -> Tablet Scoring"). Inside a table already titled "Required
// Studies", printing it again is noise, so strip the prefix when it matches.
const ARROW = " \u2192 ";

export function displayLabel(label: string, groupTitle: string): string {
  const prefix = `${groupTitle}${ARROW}`;
  if (label.toLowerCase().startsWith(prefix.toLowerCase())) return label.slice(prefix.length);
  // Some labels carry the arrow without naming the current group ("BE Strategy
  // -> Proposed Strategy" inside "Product Specific..."); keep those whole.
  return label;
}

/**
 * Regroup the wire's sections into the printed form's tables.
 *
 * Every cell on the wire lands in exactly one group: a cell the template map
 * knows takes its printed position, and anything else (a new backend cell, a
 * test fixture, a renamed id) falls through to a group named after its wire
 * section, appended in wire order. Nothing is ever silently dropped.
 */
export function groupCells(sections: WhitepaperSectionData[]): FormGroup[] {
  const byId = new Map<string, WhitepaperCell>();
  for (const section of sections) {
    for (const cell of section.cells) byId.set(cell.id, cell);
  }

  const groups: FormGroup[] = [];
  const index = new Map<string, FormGroup>();
  const claimed = new Set<string>();

  for (const spec of FORM_TABLES) {
    const rows: FormRow[] = [];
    for (const slot of spec.cells) {
      const cell = byId.get(slot.id);
      if (!cell) continue;
      claimed.add(slot.id);
      rows.push({ cell, label: displayLabel(cell.label, spec.title) });
    }
    if (rows.length === 0) continue;
    const group = { title: spec.title, rows };
    groups.push(group);
    index.set(spec.title.toLowerCase(), group);
  }

  for (const section of sections) {
    for (const cell of section.cells) {
      if (claimed.has(cell.id)) continue;
      claimed.add(cell.id);
      const key = section.title.toLowerCase();
      let group = index.get(key);
      if (!group) {
        group = { title: section.title, rows: [] };
        groups.push(group);
        index.set(key, group);
      }
      group.rows.push({ cell, label: displayLabel(cell.label, group.title) });
    }
  }

  return groups;
}

/** The blank form: the template's tables with no data behind them. */
export function blankGroups(): { title: string; labels: string[] }[] {
  return FORM_TABLES.map((t) => ({ title: t.title, labels: t.cells.map((c) => c.label) }));
}

export interface FormRef {
  n: number;
  ev: WhitepaperEvidence;
  /** Printed labels of every cell that cites this record -- the .docx appendix mapping. */
  citedBy: string[];
}

export interface FormRefs {
  refs: FormRef[];
  /** cell id -> the reference numbers printed after its value. */
  byCell: Map<string, number[]>;
}

function refKey(ev: WhitepaperEvidence): string {
  return [ev.source, ev.locator, ev.page ?? "", ev.section ?? ""].join("|");
}

/**
 * Number every distinct evidence record once, in document order, the way a
 * footnote apparatus does. One Orange Book fetch cited by nine cells is one
 * entry in the appendix with nine names against it -- which is exactly the
 * cell -> source mapping the .docx provenance table prints.
 */
export function buildRefs(groups: FormGroup[]): FormRefs {
  const refs: FormRef[] = [];
  const seen = new Map<string, FormRef>();
  const byCell = new Map<string, number[]>();

  for (const group of groups) {
    for (const row of group.rows) {
      const numbers: number[] = [];
      for (const ev of row.cell.evidence) {
        const key = refKey(ev);
        let ref = seen.get(key);
        if (!ref) {
          ref = { n: refs.length + 1, ev, citedBy: [] };
          seen.set(key, ref);
          refs.push(ref);
        }
        if (!ref.citedBy.includes(row.label)) ref.citedBy.push(row.label);
        if (!numbers.includes(ref.n)) numbers.push(ref.n);
      }
      if (numbers.length > 0) byCell.set(row.cell.id, numbers);
    }
  }

  return { refs, byCell };
}

export interface FormTally {
  populated: number;
  absent: number;
  pending: number;
  total: number;
}

export function tallyGroups(groups: FormGroup[]): FormTally {
  let populated = 0;
  let absent = 0;
  let pending = 0;
  for (const group of groups) {
    for (const row of group.rows) {
      if (row.cell.status === "populated") populated += 1;
      else if (row.cell.status === "verified_absent") absent += 1;
      else pending += 1;
    }
  }
  return { populated, absent, pending, total: populated + absent + pending };
}
