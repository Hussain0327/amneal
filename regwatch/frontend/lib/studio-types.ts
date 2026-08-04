// Domain types for the Compliance Studio surface (/studio).
//
// The whole surface is built on one anchoring idea: a finding is not a report
// line, it is a span of the document. Every finding carries (blockId, start,
// end) so it can be highlighted in place, ticked on the compliance spine, and
// invalidated when the analyst edits the text under it. Anything that cannot be
// anchored is not a finding; it is a note about the document as a whole.

/** Severity of a compliance finding, most to least serious. */
export type Severity = "critical" | "major" | "minor" | "info";

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
   * True once the analyst has edited the block this finding points at. The
   * finding stays visible but stops counting toward the verdict: the text it
   * described no longer exists.
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
  tone: "blocking" | "review" | "clear" | "unchecked";
  /** Short verdict for the panel header, e.g. "Not submission-ready". */
  label: string;
  /** Critical findings. Any one of these blocks submission. */
  blocking: number;
  /** Major + minor findings. Resolve before filing, not blocking on their own. */
  toResolve: number;
  /** Info findings. Observations, nothing to fix. */
  notes: number;
  /** Findings whose anchored text was edited after the check ran. */
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
