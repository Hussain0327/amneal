// Domain types for the Compliance Studio surface (/studio).
//
// The whole surface is built on one anchoring idea: a finding is not a report
// line, it is a span of the document. Every finding carries (blockId, start,
// end) so it can be highlighted in place, ticked on the compliance spine, and
// invalidated when the analyst edits the text under it. Anything that cannot be
// anchored is not a finding; it is a note about the document as a whole.

/** Severity of a compliance finding, most to least serious. */
export type Severity = "critical" | "major" | "minor" | "info";

/**
 * How a reviewer closed a finding. Exactly one of these, or the finding is open.
 *
 * "fixed_elsewhere" is not a softer "fixed": it is the case where the remedy
 * landed somewhere this finding does not point at -- a definitions table added
 * before the section, a cross-reference added to a different document. Two of
 * the checks in this repository name that path in their own text, and without a
 * word for it the only way to close them is a false "not applicable".
 */
export type Disposition = "fixed" | "fixed_elsewhere" | "not_applicable" | "disputed";

/** Why a disposition was refused. Rendered to the analyst, never swallowed. */
export type DispositionError =
  | "unknown_finding"
  | "check_in_flight"
  | "not_editable"
  | "fix_not_evidenced"
  | "justification_required"
  | "justification_too_long";

/** Why applying a suggested fix was refused. */
export type ApplyError = "unknown_finding" | "no_suggestion" | "not_editable" | "anchor_moved";

/** How long a justification may be. Enforced in canDispose, quoted in the error. */
export const JUSTIFICATION_MAX = 600;

/**
 * One recorded judgement on a finding.
 *
 * Append-only: changing your mind writes a new entry rather than overwriting the
 * last, so an earlier judgement can always be read back. That is the shape a
 * controlled record has to have, and it is the shape the real endpoint will have
 * to meet.
 *
 * What this is NOT: `at` is the client clock and `by` is whoever the browser
 * session believes it is. There is no server timestamp and no electronic
 * signature here, so a record built in this surface is a working note, not a
 * 21 CFR Part 11 record. The panel says so in as many words.
 */
export interface FindingRecord {
  disposition: Disposition;
  /** Required for every disposition except "fixed". Trimmed at the boundary. */
  justification: string;
  /** Client clock, ISO-8601, injected by the caller. Never read from inside the model. */
  at: string;
  /** Who the surface believes recorded it. There is no authenticated session here. */
  by: string;
  /** The anchored text as the checker read it, copied from Finding.excerpt. */
  excerpt: string;
}

/** Structural role of a document block. Drives typography, not semantics. */
export type BlockType = "title" | "meta" | "h2" | "p" | "table";

/**
 * Why a run of characters is marked. "insert" is the tracked-change span: the
 * region of a block that differs from the version the analyst opened. It is a
 * changed-region marker, not a per-character diff -- several edits to one block
 * collapse into the single span that contains them all.
 */
export type MarkKind = "finding" | "highlight" | "insert";

/**
 * Where a document stands relative to the compliance checker.
 * `stale` means it was checked and then edited: prior findings may no longer
 * describe the text, so the UI must stop presenting them as current.
 */
export type CheckState = "unchecked" | "checking" | "checked" | "stale";

/** Which slide-out panel the activity rail has open, if any. */
export type PanelId = "assistant" | "findings";

/** A run of characters inside one block, in block-text character offsets. */
export interface Mark {
  start: number;
  end: number;
  kind: MarkKind;
  /** Set when kind is "finding"; links the span back to its Finding. */
  findingId?: string;
}

/** One row of a document table. */
export interface TableRow {
  cells: string[];
  head?: boolean;
}

/**
 * One editable unit of the document. Text is the single source of truth for
 * offsets; marks index into it. Tables are rendered read-only in this cut
 * (see rows) because cell-level offsets need an editor engine we have not taken
 * on yet.
 */
export interface Block {
  id: string;
  type: BlockType;
  text: string;
  marks: Mark[];
  rows?: TableRow[];
  /**
   * The text as it stood when the analyst opened the document. Captured on the
   * first edit and never overwritten, so tracked changes always diff against
   * the released version rather than against the previous keystroke.
   */
  original?: string;
  /**
   * The text as the last check read it. Finding offsets are in THIS coordinate
   * space, which is what makes the staleness rule exact rather than a guess.
   *
   * Deliberately not the same field as `original`. That one is text-at-open and
   * serves tracked changes; this one is text-at-check and serves the evidence
   * rule. They diverge the moment an analyst edits before running a check, so
   * one field cannot do both jobs.
   */
  checkedText?: string;
}

/** A compliance finding anchored to a span of one block. */
export interface Finding {
  id: string;
  severity: Severity;
  /** Short, specific, sentence case. Names the defect, not the rule. */
  title: string;
  /** What is wrong and what would resolve it. */
  detail: string;
  blockId: string;
  start: number;
  end: number;
  /** Human-readable position, e.g. "Section 2". Display only. */
  location: string;
  /** The standard or SOP the check ran against, e.g. "ICH Q10; 21 CFR 211.100". */
  standard: string;
  /**
   * The exact text at [start, end) when the check ran. Recomputed by
   * applyFindings from the block itself, so an API-supplied span can never
   * disagree with the text it claims to quote, and an apply can tell whether it
   * is still aimed at what the checker saw.
   */
  excerpt: string;
  /**
   * What to write instead, replacing [start, end).
   *
   * Absent when the fix needs a fact only the analyst has -- an approver name,
   * an effective date, a validation report number. A fabricated suggestion in a
   * GMP-controlled document is worse than no suggestion, so those findings carry
   * none rather than something plausible.
   */
  suggestion?: string;
  /** Every judgement recorded on this finding, oldest first. Current is the last. */
  records?: FindingRecord[];
  /**
   * True when a check ran again and STILL reported this finding after it had
   * been recorded fixed. The finding re-opens and the record is kept: dropping
   * either side of that contradiction is the audit lie.
   */
  contested?: boolean;
  /**
   * True when the analyst's edits touched this finding's as-checked span.
   *
   * Derived, never set by hand: recomputed from Block.checkedText on every write,
   * so typing an edit back out again clears it. It carries two meanings at once,
   * and they are the same fact -- the claim no longer describes the text, and
   * there is now evidence that a fix was attempted here.
   */
  stale?: boolean;
}

/** A document in the repository. */
export interface StudioDoc {
  id: string;
  /** File name as it appears in the tree, e.g. "3.2.S.4.1 Specification.docx". */
  name: string;
  /** Breadcrumb path, e.g. "Module 3 / 3.2.S Drug Substance". */
  path: string;
  version: string;
  blocks: Block[];
  findings: Finding[];
  checkState: CheckState;
  /** Standards this document is checked against. Shown on the findings header. */
  standards: string[];
  /**
   * Findings that carried a record and were not returned by the latest check.
   * The defect is gone; the judgement that closed it still has to be printable,
   * so it moves here rather than being dropped on the floor.
   */
  closed?: Finding[];
}

export interface TreeFolder {
  kind: "folder";
  id: string;
  label: string;
  /** Corpus tag, e.g. "CTD" or "SOP". */
  badge?: string;
  children: TreeNode[];
}

export interface TreeDocRef {
  kind: "doc";
  id: string;
  docId: string;
}

export type TreeNode = TreeFolder | TreeDocRef;

/**
 * The document's compliance standing. Deliberately not a 0-100 score: no such
 * scale exists in CMC review, and inventing one would present a guess as a
 * measurement. Counts and a verdict are what a reviewer actually acts on.
 */
export interface Verdict {
  tone: "blocking" | "review" | "clear" | "settled" | "unchecked";
  /** Short verdict for the panel header, e.g. "Not submission-ready". */
  label: string;
  /** Critical findings. Any one of these blocks submission. */
  blocking: number;
  /** Major + minor findings. Resolve before filing, not blocking on their own. */
  toResolve: number;
  /** Info findings. Observations, nothing to fix. */
  notes: number;
  /** Findings closed by a recorded disposition. */
  disposed: number;
  /** Findings whose anchored text was edited after the check ran, and not recorded. */
  stale: number;
}

/** One message in the assistant thread. */
export interface AssistantMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** Sources under an assistant message. Never present on a user message. */
  sources?: string[];
  /** True while the reply is still typing in. */
  streaming?: boolean;
}

/** An action offered on the floating toolbar when text is selected. */
export type SelectionAction = "highlight" | "summarize" | "explain" | "check" | "ask";
