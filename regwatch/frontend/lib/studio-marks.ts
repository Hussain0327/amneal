// Pure text-span logic for the Compliance Studio.
//
// Everything here operates on (start, end) character offsets into a block's
// plain text. Keeping it free of React and of the DOM (except the two helpers
// that explicitly take an element) is what makes the anchoring testable: the
// spine, the highlights, and the staleness rule all reduce to these functions.

import type { Block, Finding, Mark, Severity, StudioDoc, Verdict } from "./studio-types";

/** Most to least serious. Used for sorting and for picking the strongest mark. */
export const SEVERITY_RANK: Record<Severity, number> = {
  critical: 0,
  major: 1,
  minor: 2,
  info: 3,
};

/** A run of text carrying zero or more overlapping marks. */
export interface Segment {
  text: string;
  marks: Mark[];
}

/**
 * Split a block's text into non-overlapping runs, each tagged with every mark
 * covering it. Overlapping marks are handled by sweeping every boundary, so a
 * user highlight laid over a finding renders as three runs, not a broken nest.
 * Out-of-range marks are clamped; empty marks are dropped.
 */
export function segmentBlock(text: string, marks: Mark[]): Segment[] {
  const clamped = marks
    .map((m) => ({
      ...m,
      start: Math.max(0, Math.min(m.start, text.length)),
      end: Math.max(0, Math.min(m.end, text.length)),
    }))
    .filter((m) => m.end > m.start);

  if (clamped.length === 0) return text ? [{ text, marks: [] }] : [];

  const bounds = new Set<number>([0, text.length]);
  for (const m of clamped) {
    bounds.add(m.start);
    bounds.add(m.end);
  }
  const points = [...bounds].sort((a, b) => a - b);

  const out: Segment[] = [];
  for (let i = 0; i < points.length - 1; i += 1) {
    const start = points[i];
    const end = points[i + 1];
    if (end <= start) continue;
    out.push({
      text: text.slice(start, end),
      marks: clamped.filter((m) => m.start <= start && m.end >= end),
    });
  }
  return out;
}

/**
 * The mark that should win when several cover the same run. Precedence is
 * highlight > finding (most severe first) > tracked change: the analyst's own
 * mark beats the machine's, and a compliance defect beats a bookkeeping note
 * about what moved.
 */
export function dominantMark(marks: Mark[], findings: Finding[]): Mark | null {
  if (marks.length === 0) return null;
  const highlight = marks.find((m) => m.kind === "highlight");
  if (highlight) return highlight;
  // Non-finding marks sort to rank 99, so any real finding outranks them.
  const byId = new Map(findings.map((f) => [f.id, f]));
  return [...marks].sort((a, b) => {
    const fa = a.findingId ? byId.get(a.findingId) : undefined;
    const fb = b.findingId ? byId.get(b.findingId) : undefined;
    const ra = fa ? SEVERITY_RANK[fa.severity] : 99;
    const rb = fb ? SEVERITY_RANK[fb.severity] : 99;
    return ra - rb;
  })[0];
}

/**
 * Rewrite marks after a block's text changed.
 *
 * The edit is localised by common prefix and suffix, which is exact for the
 * single-caret edits contentEditable produces. A mark entirely before or after
 * the edit survives (shifted); a mark the edit cut into is dropped rather than
 * guessed at. Findings are not remapped by this function at all -- editing the
 * text under a finding invalidates it (see staleFindings), which is the honest
 * outcome, not a shifted offset.
 */
export function remapMarks(oldText: string, newText: string, marks: Mark[]): Mark[] {
  if (oldText === newText) return marks;

  const max = Math.min(oldText.length, newText.length);
  let prefix = 0;
  while (prefix < max && oldText[prefix] === newText[prefix]) prefix += 1;

  let suffix = 0;
  while (
    suffix < max - prefix &&
    oldText[oldText.length - 1 - suffix] === newText[newText.length - 1 - suffix]
  ) {
    suffix += 1;
  }

  const oldEnd = oldText.length - suffix; // exclusive end of the replaced run
  const delta = newText.length - oldText.length;

  const out: Mark[] = [];
  for (const m of marks) {
    if (m.end <= prefix) {
      out.push(m); // entirely before the edit
    } else if (m.start >= oldEnd) {
      out.push({ ...m, start: m.start + delta, end: m.end + delta }); // entirely after
    }
    // otherwise the edit landed inside the mark: drop it rather than invent a span
  }
  return out;
}

/**
 * Mark every finding anchored in `blockId` as stale, and drop the finding marks
 * from that block. Called when the analyst edits a block: the checker's claim
 * was about text that no longer exists, so it must stop counting as current.
 */
export function staleFindings(doc: StudioDoc, blockId: string): StudioDoc {
  const touched = doc.findings.some((f) => f.blockId === blockId && !f.stale);
  if (!touched) return doc;
  return {
    ...doc,
    checkState: "stale",
    findings: doc.findings.map((f) => (f.blockId === blockId ? { ...f, stale: true } : f)),
  };
}

/** Replace one block's text, remapping its highlights and staling its findings. */
export function applyEdit(doc: StudioDoc, blockId: string, nextText: string): StudioDoc {
  const block = doc.blocks.find((b) => b.id === blockId);
  if (!block || block.text === nextText) return doc;

  const kept = remapMarks(block.text, nextText, block.marks).filter((m) => m.kind === "highlight");
  const withText: StudioDoc = {
    ...doc,
    blocks: doc.blocks.map((b) =>
      b.id === blockId
        ? // Capture the released text once, on the first edit -- tracked changes
          // must diff against the version the analyst opened, not the last keystroke.
          { ...b, original: b.original ?? b.text, text: nextText, marks: kept }
        : b,
    ),
  };
  return staleFindings(withText, blockId);
}

/**
 * The tracked-change span for a block: the region that differs from the text
 * the analyst opened. Localised by common prefix and suffix, so it is the
 * smallest run containing every edit rather than a per-character diff. A pure
 * deletion yields nothing -- there is no remaining text to mark, and rendering
 * removed words would mean carrying them in the model, which this cut does not.
 */
export function changeMarks(block: Block): Mark[] {
  const original = block.original;
  if (original === undefined || original === block.text) return [];

  const text = block.text;
  const max = Math.min(original.length, text.length);
  let prefix = 0;
  while (prefix < max && original[prefix] === text[prefix]) prefix += 1;

  let suffix = 0;
  while (
    suffix < max - prefix &&
    original[original.length - 1 - suffix] === text[text.length - 1 - suffix]
  ) {
    suffix += 1;
  }

  const end = text.length - suffix;
  return end > prefix ? [{ start: prefix, end, kind: "insert" }] : [];
}

/** Add a user highlight over a span, merging into any highlight it touches. */
export function addHighlight(doc: StudioDoc, blockId: string, start: number, end: number): StudioDoc {
  if (end <= start) return doc;
  return {
    ...doc,
    blocks: doc.blocks.map((b) => {
      if (b.id !== blockId) return b;
      const others = b.marks.filter((m) => m.kind !== "highlight");
      const highlights = b.marks.filter((m) => m.kind === "highlight");
      let lo = start;
      let hi = end;
      const disjoint: Mark[] = [];
      for (const h of highlights) {
        if (h.start <= hi && h.end >= lo) {
          lo = Math.min(lo, h.start);
          hi = Math.max(hi, h.end);
        } else {
          disjoint.push(h);
        }
      }
      return { ...b, marks: [...others, ...disjoint, { start: lo, end: hi, kind: "highlight" as const }] };
    }),
  };
}

/** Remove every user highlight from the document. Findings are untouched. */
export function clearHighlights(doc: StudioDoc): StudioDoc {
  return {
    ...doc,
    blocks: doc.blocks.map((b) => ({ ...b, marks: b.marks.filter((m) => m.kind !== "highlight") })),
  };
}

/** Attach finding marks to the blocks they anchor into. Used when a check lands. */
export function applyFindings(doc: StudioDoc, findings: Finding[]): StudioDoc {
  const byBlock = new Map<string, Finding[]>();
  for (const f of findings) {
    byBlock.set(f.blockId, [...(byBlock.get(f.blockId) ?? []), f]);
  }
  return {
    ...doc,
    checkState: "checked",
    findings,
    blocks: doc.blocks.map((b) => {
      const own = byBlock.get(b.id) ?? [];
      const highlights = b.marks.filter((m) => m.kind === "highlight");
      const findingMarks: Mark[] = own.map((f) => ({
        start: f.start,
        end: f.end,
        kind: "finding" as const,
        findingId: f.id,
      }));
      return { ...b, marks: [...findingMarks, ...highlights] };
    }),
  };
}

/**
 * The document's standing. Stale findings are counted separately and excluded
 * from the verdict: a claim about deleted text is not evidence of compliance
 * either way.
 */
export function verdictFor(doc: StudioDoc): Verdict {
  if (doc.checkState === "unchecked" || doc.checkState === "checking") {
    return { tone: "unchecked", label: "Not checked yet", blocking: 0, toResolve: 0, notes: 0, stale: 0 };
  }

  const live = doc.findings.filter((f) => !f.stale);
  const stale = doc.findings.length - live.length;
  const blocking = live.filter((f) => f.severity === "critical").length;
  const toResolve = live.filter((f) => f.severity === "major" || f.severity === "minor").length;
  const notes = live.filter((f) => f.severity === "info").length;

  if (blocking > 0) return { tone: "blocking", label: "Not submission-ready", blocking, toResolve, notes, stale };
  if (toResolve > 0) return { tone: "review", label: "Resolve before filing", blocking, toResolve, notes, stale };
  return { tone: "clear", label: "No open findings", blocking, toResolve, notes, stale };
}

/** Findings sorted for display: severity first, then document order. */
export function sortFindings(doc: StudioDoc): Finding[] {
  const order = new Map(doc.blocks.map((b, i) => [b.id, i]));
  return [...doc.findings].sort((a, b) => {
    if (a.stale !== b.stale) return a.stale ? 1 : -1;
    const rank = SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
    if (rank !== 0) return rank;
    return (order.get(a.blockId) ?? 0) - (order.get(b.blockId) ?? 0);
  });
}

/**
 * The tri-state glyph a file gets in the tree. Mirrors the White Paper cell
 * vocabulary already shipped in globals.css: solid = settled, hollow ring =
 * nothing recorded, clay ring = needs a human.
 */
export function docGlyph(doc: StudioDoc): "clean" | "findings" | "unchecked" | "checking" {
  if (doc.checkState === "checking") return "checking";
  if (doc.checkState === "unchecked" || doc.checkState === "stale") return "unchecked";
  return doc.findings.some((f) => !f.stale && f.severity !== "info") ? "findings" : "clean";
}

// ---------------------------------------------------------------------------
// DOM helpers. These take an element explicitly so the rest stays pure.
// ---------------------------------------------------------------------------

/**
 * Character offset of a DOM position within `root`'s text, counting through
 * whatever <mark> wrappers the renderer produced. Uses a Range rather than a
 * tree walk so element-boundary positions resolve correctly too.
 */
export function offsetWithin(root: HTMLElement, container: Node, offset: number): number {
  if (!root.contains(container)) return 0;
  const range = root.ownerDocument.createRange();
  range.selectNodeContents(root);
  try {
    range.setEnd(container, offset);
  } catch {
    return 0;
  }
  return range.toString().length;
}

/** The selected span inside `root`, or null when the selection is empty or outside. */
export function selectionOffsets(root: HTMLElement, selection: Selection | null): { start: number; end: number } | null {
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const start = offsetWithin(root, range.startContainer, range.startOffset);
  const end = offsetWithin(root, range.endContainer, range.endOffset);
  if (end <= start) return null;
  return { start, end };
}

/** Truncate a quoted selection for use in an assistant prompt. */
export function quote(text: string, limit = 180): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1)}...`;
}

/** Plain text of a block, used for quoting a selection back to the assistant. */
export function sliceBlock(block: Block, start: number, end: number): string {
  return block.text.slice(Math.max(0, start), Math.max(0, end));
}
