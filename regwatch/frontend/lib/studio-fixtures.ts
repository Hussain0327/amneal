// Fixture repository for the Compliance Studio -- and the definitive map of
// which side of the fixture/backend line each Studio feature lives on.
//
// FIXTURE (this file; no network):
// - the eleven working draft documents (DOCS) and the repository tree (TREE);
// - findings (CHECK_RESULTS), applied by the page after a fake CHECK_MS
//   delay -- the backend POST /studio/check + poll GET /studio/check/{id}
//   exists and is tested, but this page does not call it yet;
// - canned assistant replies (assistantReply) for questions about the
//   working repository.
//
// BACKEND-BACKED (real endpoints, called from app/studio/page.tsx):
// - the PSG reference rail: GET /psg/documents and
//   /psg/documents/{id}/{content|requirements|pdf|docx};
// - reference-PSG checks: fetchPsgRequirements anchors the requirements
//   ingest extracted from that exact PSG version;
// - assistant Q&A over an open reference PSG: askQuery (/query), scoped to
//   that PSG's drug, with server-validated citations.
//
// Before adding a feature here, decide which side of this line it lives on;
// there is no drafts/document service to find.
//
// Content is representative CMC material for a generic ER tablet. It is sample
// data, not an Amneal submission.
//
// Only three of the eleven findings carry a `suggestion`, and that ratio is the
// point rather than an omission. A suggestion is offered when the remedy is
// carried entirely in the words -- expand an abbreviation, name the staged
// procedure, state the shelf life that another document in this repository
// already proposes. It is withheld whenever the fix needs a fact only the
// analyst holds: an approver, an effective date, a validation report number, a
// sampling interval, a scale-up justification. Inventing any of those would put
// a plausible falsehood into a GMP-controlled record, which is worse than
// leaving the analyst to type it.

import type { Block, Finding, StudioDoc, TreeNode } from "./studio-types";

// ---------------------------------------------------------------------------
// Anchoring
// ---------------------------------------------------------------------------

/**
 * Resolve a finding's span by searching the block text, so fixture offsets can
 * never drift out of sync with the prose above them. Throws at module load if
 * the needle is missing, which turns a silent mis-anchored highlight into an
 * immediate, obvious failure.
 */
function anchor(
  blocks: Block[],
  blockId: string,
  needle: string,
): { blockId: string; start: number; end: number; excerpt: string } {
  const block = blocks.find((b) => b.id === blockId);
  if (!block) throw new Error(`studio-fixtures: no block "${blockId}"`);
  const start = block.text.indexOf(needle);
  if (start < 0) throw new Error(`studio-fixtures: "${needle}" not found in block "${blockId}"`);
  // The needle IS the excerpt, so a span and the text it claims to quote cannot
  // disagree here by construction. applyFindings recomputes it for API findings,
  // where that guarantee has to be enforced rather than assumed.
  return { blockId, start, end: start + needle.length, excerpt: needle };
}

function block(id: string, type: Block["type"], text: string, rows?: Block["rows"]): Block {
  return { id, type, text, marks: [], rows };
}

// ---------------------------------------------------------------------------
// 3.2.S.4.1 Specification -- Drug Substance (already checked)
// ---------------------------------------------------------------------------

const dsSpecBlocks: Block[] = [
  block("ds-1", "title", "3.2.S.4.1 Specification - Drug Substance"),
  block("ds-2", "meta", "Document ID: CMC-DS-SPEC-0007  |  Version: 5.0"),
  block("ds-3", "h2", "1. Acceptance Criteria"),
  block(
    "ds-4",
    "p",
    "Assay (on anhydrous basis) 98.0% - 102.0%; any unspecified impurity NMT 0.10%; total impurities NMT 0.5%; residue on ignition NMT 0.1%.",
  ),
  block("ds-5", "h2", "2. Control of Elemental Impurities"),
  block(
    "ds-6",
    "p",
    "A risk assessment per ICH Q3D(R2) concluded that no routine testing for elemental impurities is required. The assessment covers the catalysts used in the final two synthetic steps and all product-contact materials in the isolation train.",
  ),
  block("ds-7", "h2", "3. Residual Solvents"),
  block(
    "ds-8",
    "p",
    "Class 2 solvents are controlled per ICH Q3C(R8) Option I. LOD is applied where a reported result falls below the quantitation limit of the method.",
  ),
  block("ds-9", "h2", "4. Microbial Limits"),
  block(
    "ds-10",
    "p",
    "Total aerobic microbial count NMT 1000 CFU/g and total combined yeasts and moulds NMT 100 CFU/g per USP <61>. Absence of Escherichia coli per USP <62>.",
  ),
];

const dsSpecFindings: Finding[] = [
  {
    id: "ds-f1",
    severity: "major",
    title: "Version control block is incomplete",
    detail:
      "The header records a version but names no approver and no effective date. A GMP-controlled document cannot be released without both.",
    location: "Header",
    standard: "ICH Q10; 21 CFR 211.100",
    ...anchor(dsSpecBlocks, "ds-2", "Version: 5.0"),
  },
  {
    id: "ds-f2",
    severity: "minor",
    title: "Abbreviation used before it is defined",
    detail:
      "LOD appears here with no expansion and the document has no definitions section. Expand it on first use or add a definitions table.",
    location: "Section 3",
    standard: "Internal SOP QA-018",
    // Expanding on first use is one of the two remedies the finding itself
    // names; the other one (a definitions table) lands in a block this finding
    // does not point at, which is what "fixed elsewhere" is for.
    suggestion: "The limit of detection (LOD)",
    ...anchor(dsSpecBlocks, "ds-8", "LOD"),
  },
  {
    id: "ds-f3",
    severity: "info",
    title: "Section numbering matches CTD granularity",
    detail: "Headings follow the ICH M4Q(R1) numbering convention for 3.2.S.4.1. Nothing to change.",
    location: "Document",
    standard: "ICH M4Q(R1)",
    ...anchor(dsSpecBlocks, "ds-3", "1. Acceptance Criteria"),
  },
];

// ---------------------------------------------------------------------------
// 3.2.P.5.1 Specification -- Drug Product
// ---------------------------------------------------------------------------

const dpSpecBlocks: Block[] = [
  block("dp-1", "title", "3.2.P.5.1 Specification - Drug Product"),
  block("dp-2", "meta", "Product: Ranolazine Extended-Release Tablets, 500 mg"),
  block("dp-3", "meta", "Document ID: CMC-DP-SPEC-0142  |  Version: 4.2  |  Effective: 14-Mar-2025"),
  block("dp-4", "h2", "1. Purpose and Scope"),
  block(
    "dp-5",
    "p",
    "This specification defines the quality attributes, analytical procedures and acceptance criteria applied to Ranolazine Extended-Release Tablets 500 mg at release and throughout the proposed shelf life. It is issued in support of Module 3.2.P.5 of the Common Technical Document.",
  ),
  block("dp-6", "h2", "2. Acceptance Criteria"),
  block("dp-7", "table", "Acceptance criteria table", [
    { cells: ["Attribute", "Analytical Procedure", "Acceptance Criteria"], head: true },
    { cells: ["Description", "Visual", "White to off-white oval film-coated tablet"] },
    { cells: ["Identification", "HPLC / IR", "Retention time corresponds to standard"] },
    { cells: ["Assay", "HPLC (Method AM-114)", "95.0% - 105.0% of label claim"] },
    { cells: ["Dissolution", "USP <711>, Apparatus 2", "Q = 80% at 60 min"] },
    { cells: ["Uniformity of dosage units", "USP <905>", "Meets requirements"] },
  ]),
  block(
    "dp-8",
    "p",
    "Dissolution is performed in 900 mL of pH 6.8 phosphate buffer at 50 rpm with sinkers. The acceptance criterion is Q = 80% at 60 min.",
  ),
  block("dp-9", "h2", "3. Reference Standards"),
  block(
    "dp-10",
    "p",
    "The ranolazine reference standard is qualified against the current USP Reference Standard lot. Working standards are re-qualified every 12 months or on receipt of a new lot, whichever comes first.",
  ),
];

const dpSpecFindings: Finding[] = [
  {
    id: "dp-f1",
    severity: "critical",
    title: "Dissolution criterion omits staged testing",
    detail:
      "The criterion states a single stage. USP <711> expects the S1/S2/S3 evaluation to be stated or explicitly cross-referenced; without it a reviewer cannot confirm how an out-of-stage result is handled.",
    location: "Section 2",
    standard: "USP <711>; ICH Q6A Decision Tree #7",
    suggestion:
      "The acceptance criterion is Q = 80% at 60 min, evaluated by the staged S1/S2/S3 procedure of USP <711>.",
    ...anchor(dpSpecBlocks, "dp-8", "The acceptance criterion is Q = 80% at 60 min."),
  },
  {
    id: "dp-f2",
    severity: "major",
    title: "Re-qualification interval carries no justification",
    detail:
      "A 12-month interval is asserted without reference to standard stability data. State the basis or cross-reference the qualification protocol.",
    location: "Section 3",
    standard: "ICH Q7 11.17",
    ...anchor(dpSpecBlocks, "dp-10", "re-qualified every 12 months"),
  },
  {
    id: "dp-f3",
    severity: "minor",
    title: "Shelf life referenced but never stated",
    detail:
      "The scope binds this specification to the shelf life without stating it or pointing at 3.2.P.8.1. Add the proposed shelf life or the cross-reference.",
    location: "Section 1",
    standard: "ICH Q1A(R2); Internal SOP QA-018",
    // 24 months is not invented: it is what 3.2.P.8.1 in this same repository
    // proposes (block st-6). A suggestion that made up a shelf life would be
    // exactly the kind of plausible fabrication this surface must not produce.
    suggestion: "throughout the proposed 24-month shelf life (see 3.2.P.8.1)",
    ...anchor(dpSpecBlocks, "dp-5", "throughout the proposed shelf life"),
  },
];

// ---------------------------------------------------------------------------
// Remaining repository documents
// ---------------------------------------------------------------------------

const dsManuBlocks: Block[] = [
  block("dm-1", "title", "3.2.S.2.2 Description of Manufacturing Process"),
  block("dm-2", "meta", "Document ID: CMC-DS-MFG-0031  |  Version: 2.1"),
  block("dm-3", "h2", "1. Process Overview"),
  block(
    "dm-4",
    "p",
    "Ranolazine drug substance is produced by a four-step convergent synthesis followed by crystallisation from isopropanol and water. The final step is carried out at commercial scale in reactor R-401.",
  ),
  block("dm-5", "h2", "2. Batch Size"),
  block(
    "dm-6",
    "p",
    "The proposed commercial batch size is 240 kg. Development batches were manufactured at 40 kg and 120 kg using the same equipment train.",
  ),
  block("dm-7", "h2", "3. Reprocessing"),
  block(
    "dm-8",
    "p",
    "Material failing the residual solvent limit may be re-slurried once in the same solvent system and re-tested against the full specification.",
  ),
];

const dsManuFindings: Finding[] = [
  {
    id: "dm-f1",
    severity: "critical",
    title: "Reprocessing described without a validation reference",
    detail:
      "A reprocessing step is permitted with no cited validation study and no limit on cumulative reprocessing. Reference the study or remove the allowance.",
    location: "Section 3",
    standard: "ICH Q7 14.20; 21 CFR 211.115",
    ...anchor(dsManuBlocks, "dm-8", "may be re-slurried once in the same solvent system"),
  },
  {
    id: "dm-f2",
    severity: "major",
    title: "Scale-up gap between development and commercial batches",
    detail:
      "Commercial scale is 2x the largest development batch. Justify the scale-up factor or cite the process validation protocol that covers it.",
    location: "Section 2",
    standard: "ICH Q11; FDA Process Validation Guidance",
    ...anchor(dsManuBlocks, "dm-6", "The proposed commercial batch size is 240 kg."),
  },
];

const dpProcBlocks: Block[] = [
  block("pp-1", "title", "3.2.P.3.3 Description of Manufacturing Process"),
  block("pp-2", "meta", "Document ID: CMC-DP-MFG-0088  |  Version: 3.0"),
  block("pp-3", "h2", "1. Unit Operations"),
  block(
    "pp-4",
    "p",
    "High-shear wet granulation, fluid-bed drying, milling, blending, compression on a rotary press, and aqueous film coating in a perforated pan.",
  ),
  block("pp-5", "h2", "2. In-Process Controls"),
  block(
    "pp-6",
    "p",
    "Blend uniformity is sampled at ten locations. Tablet hardness, thickness and weight are monitored at defined intervals throughout compression.",
  ),
];

const dpProcFindings: Finding[] = [
  {
    id: "pp-f1",
    severity: "major",
    title: "In-process control intervals are not specified",
    detail:
      '"Defined intervals" gives a reviewer nothing to assess. State the sampling frequency and the action limits, or cite the batch record section that does.',
    location: "Section 2",
    standard: "21 CFR 211.110; ICH Q8(R2)",
    ...anchor(dpProcBlocks, "pp-6", "at defined intervals throughout compression"),
  },
];

const dpStabBlocks: Block[] = [
  block("st-1", "title", "3.2.P.8.1 Stability Summary and Conclusions"),
  block("st-2", "meta", "Document ID: CMC-DP-STAB-0219  |  Version: 1.4"),
  block("st-3", "h2", "1. Study Design"),
  block(
    "st-4",
    "p",
    "Three primary batches were placed on long-term storage at 25 C / 60% RH and on accelerated storage at 40 C / 75% RH per ICH Q1A(R2).",
  ),
  block("st-5", "h2", "2. Conclusion"),
  block(
    "st-6",
    "p",
    "Based on 12 months of long-term and 6 months of accelerated data, a shelf life of 24 months is proposed for the commercial pack.",
  ),
];

const dpStabFindings: Finding[] = [
  {
    id: "st-f1",
    severity: "major",
    title: "Proposed shelf life exceeds what the data extrapolates to",
    detail:
      "12 months of long-term data supports extrapolation to 24 months only where accelerated data shows no significant change and the extrapolation is justified per ICH Q1E. Add that justification.",
    location: "Section 2",
    standard: "ICH Q1E",
    ...anchor(dpStabBlocks, "st-6", "a shelf life of 24 months is proposed"),
  },
  {
    id: "st-f2",
    severity: "info",
    title: "Storage conditions match ICH climatic zone II",
    detail: "Long-term and accelerated conditions are the expected pair for the stated markets. Nothing to change.",
    location: "Section 1",
    standard: "ICH Q1A(R2)",
    ...anchor(dpStabBlocks, "st-4", "25 C / 60% RH"),
  },
];

const am114Blocks: Block[] = [
  block("am-1", "title", "AM-114 Assay of Ranolazine by HPLC"),
  block("am-2", "meta", "Document ID: SOP-AM-114  |  Version: 6.0  |  Effective: 02-Feb-2025"),
  block("am-3", "h2", "1. Scope"),
  block(
    "am-4",
    "p",
    "This method determines ranolazine content in extended-release tablets by reversed-phase HPLC with UV detection at 272 nm.",
  ),
  block("am-5", "h2", "2. System Suitability"),
  block(
    "am-6",
    "p",
    "Tailing factor NMT 2.0, theoretical plates NLT 2000, and RSD of five replicate injections NMT 2.0%.",
  ),
];

const am114Findings: Finding[] = [
  {
    id: "am-f1",
    severity: "minor",
    title: "Method validation is not cross-referenced",
    detail:
      "The method states suitability limits but never points at the validation report that established them. Add the report number.",
    location: "Section 2",
    standard: "ICH Q2(R2)",
    ...anchor(am114Blocks, "am-6", "RSD of five replicate injections NMT 2.0%"),
  },
];

const qa018Blocks: Block[] = [
  block("qa-1", "title", "QA-018 Preparation and Control of GMP Documents"),
  block("qa-2", "meta", "Document ID: SOP-QA-018  |  Version: 9.2  |  Effective: 11-Nov-2024"),
  block("qa-3", "h2", "1. Header Requirements"),
  block(
    "qa-4",
    "p",
    "Every controlled document carries a document ID, version, effective date, author and approver in the header block. A document without an approver is not released.",
  ),
  block("qa-5", "h2", "2. Abbreviations"),
  block(
    "qa-6",
    "p",
    "Abbreviations are expanded on first use in each document, or collected in a definitions section placed before the first technical section.",
  ),
];

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

function doc(
  id: string,
  name: string,
  path: string,
  version: string,
  blocks: Block[],
  standards: string[],
): StudioDoc {
  return { id, name, path, version, blocks, findings: [], checkState: "unchecked", standards };
}

const ICH_USP_CFR = ["ICH Q6A", "ICH Q10", "USP <711>", "21 CFR 211", "Internal SOP QA-018"];

export const DOCS: StudioDoc[] = [
  doc(
    "ds-spec",
    "3.2.S.4.1 Specification.docx",
    "Module 3 / 3.2.S Drug Substance",
    "5.0",
    dsSpecBlocks,
    ICH_USP_CFR,
  ),
  doc("ds-manu", "3.2.S.2.2 Manufacture.docx", "Module 3 / 3.2.S Drug Substance", "2.1", dsManuBlocks, [
    "ICH Q7",
    "ICH Q11",
    "21 CFR 211",
  ]),
  doc(
    "dp-spec",
    "3.2.P.5.1 Specification.docx",
    "Module 3 / 3.2.P Drug Product",
    "4.2",
    dpSpecBlocks,
    ICH_USP_CFR,
  ),
  doc("dp-proc", "3.2.P.3.3 Manufacturing Process.docx", "Module 3 / 3.2.P Drug Product", "3.0", dpProcBlocks, [
    "ICH Q8(R2)",
    "21 CFR 211",
  ]),
  doc("dp-stab", "3.2.P.8.1 Stability Summary.docx", "Module 3 / 3.2.P Drug Product", "1.4", dpStabBlocks, [
    "ICH Q1A(R2)",
    "ICH Q1E",
  ]),
  doc("am-114", "AM-114 HPLC Assay.docx", "Analytical Methods", "6.0", am114Blocks, ["ICH Q2(R2)", "USP <621>"]),
  doc("qa-018", "QA-018 Document Control.docx", "Analytical Methods", "9.2", qa018Blocks, ["21 CFR 211.100"]),
];

/**
 * What the compliance pipeline returns for each document. Stands in for the
 * API: the studio applies these when the analyst runs a check. A document with
 * no entry comes back clean, which is a real outcome the UI must handle.
 */
export const CHECK_RESULTS: Record<string, Finding[]> = {
  "ds-spec": dsSpecFindings,
  "ds-manu": dsManuFindings,
  "dp-spec": dpSpecFindings,
  "dp-proc": dpProcFindings,
  "dp-stab": dpStabFindings,
  "am-114": am114Findings,
  "qa-018": [],
};

/** Document opened when the studio first loads. */
export const INITIAL_DOC_ID = "ds-spec";

export const TREE: TreeNode[] = [
  {
    kind: "folder",
    id: "module-3",
    label: "Module 3 - Quality",
    badge: "CTD",
    children: [
      {
        kind: "folder",
        id: "3-2-s",
        label: "3.2.S Drug Substance",
        children: [
          { kind: "doc", id: "t-ds-spec", docId: "ds-spec" },
          { kind: "doc", id: "t-ds-manu", docId: "ds-manu" },
        ],
      },
      {
        kind: "folder",
        id: "3-2-p",
        label: "3.2.P Drug Product",
        children: [
          { kind: "doc", id: "t-dp-spec", docId: "dp-spec" },
          { kind: "doc", id: "t-dp-proc", docId: "dp-proc" },
          { kind: "doc", id: "t-dp-stab", docId: "dp-stab" },
        ],
      },
    ],
  },
  {
    kind: "folder",
    id: "analytical",
    label: "Analytical Methods",
    badge: "SOP",
    children: [
      { kind: "doc", id: "t-am-114", docId: "am-114" },
      { kind: "doc", id: "t-qa-018", docId: "qa-018" },
    ],
  },
];

// ---------------------------------------------------------------------------
// Assistant
// ---------------------------------------------------------------------------

export const ASSISTANT_INTRO =
  "I can answer questions about any document in this repository and the guidelines it has to satisfy. I never change your text.";

export interface CannedReply {
  text: string;
  sources: string[];
}

/**
 * Stand-in for the Q&A stream, keyed loosely on what the analyst asked.
 *
 * Every reply returns an empty `sources` list deliberately. These answers are
 * written, not retrieved: the standards they discuss (USP, ICH, 21 CFR, the
 * internal SOPs) are in no corpus this system ingests, so naming them as
 * citations would put an invented provenance in the same panel that shows
 * server-validated page references whenever a reference PSG is open. An empty
 * list renders as "No source in this repository.", which is the honest reading
 * of a canned answer.
 */
export function assistantReply(prompt: string, docName: string, selection?: string): CannedReply {
  const q = prompt.toLowerCase();

  if (q.startsWith("summarize") || q.startsWith("summarise")) {
    return {
      text: selection
        ? `That passage sets one acceptance criterion and the conditions it is measured under. It states the value and the method but not how an out-of-limit result is escalated, which is usually carried in the linked SOP rather than the specification itself.`
        : `${docName} defines the quality attributes, the analytical procedures used to measure them, and the acceptance criteria applied at release and through shelf life. The controlling sections are the acceptance criteria table and the reference standard qualification.`,
      sources: [],
    };
  }

  if (q.startsWith("explain")) {
    return {
      text: `USP <711> defines dissolution acceptance as a staged evaluation: six units at S1, six more at S2 if S1 fails, and twelve more at S3. Stating only the S1 value leaves a reviewer unable to confirm how an out-of-stage result is handled, which is why the staged table is normally reproduced or cross-referenced directly beneath the criterion.`,
      sources: [],
    };
  }

  if (q.startsWith("check")) {
    return {
      text: `Against USP <711> this passage is incomplete: the criterion names one stage where the standard expects three. Against ICH Q6A it is consistent -- a single-point criterion is acceptable for an extended-release product only when the staged evaluation is referenced, and that reference is missing here.`,
      sources: [],
    };
  }

  if (q.includes("shelf life") || q.includes("stability")) {
    return {
      text: `The stability summary proposes 24 months from 12 months of long-term and 6 months of accelerated data. ICH Q1E permits extrapolation to twice the long-term period, capped at long-term plus 12 months, but only where the accelerated data shows no significant change and the extrapolation is justified in the submission. That justification is not present.`,
      sources: [],
    };
  }

  if (q.includes("approver") || q.includes("header") || q.includes("version control")) {
    return {
      text: `Internal SOP QA-018 requires a document ID, version, effective date, author and approver in the header block of every controlled document, and states that a document without an approver is not released. This header carries the ID and version only.`,
      sources: [],
    };
  }

  return {
    text: `I cannot answer that from this repository. I have the seven documents in the tree and the ICH, USP and 21 CFR guidance they are checked against. Ask about a specification, a method, a stability conclusion, or the rule behind any finding, and I will cite the passage it comes from.`,
    sources: [],
  };
}
