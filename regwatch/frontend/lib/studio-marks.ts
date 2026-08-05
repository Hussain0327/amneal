// Pure text-span and disposition logic for the Compliance Studio.
//
// Everything here operates on (start, end) character offsets into a block's
// plain text. Keeping it free of React and of the DOM (except the three helpers
// that explicitly take an element) is what makes the anchoring testable: the
// spine, the highlights, the staleness rule and the whole record loop reduce to
// these functions.
//
// TWO COORDINATE SPACES, and the difference is load-bearing:
//
//   Block.original    the text when the analyst OPENED the document. Tracked
//                     changes diff against this. Never overwritten.
//   Block.checkedText the text when the checker last READ the block. Finding
//                     offsets live in this space, so it is the only honest
//                     baseline for "has the analyst changed what this finding
//                     points at".
//
// They diverge the moment somebody edits before running a check, which is why
// one field cannot do both jobs.
//
// NO AMBIENT I/O. This module never reads a clock, a random source, or a
// browser global. A record's timestamp and author are arguments, because a
// module that stamps its own time cannot be tested and cannot be trusted about
// when something happened.

import type {
  ApplyError,
  Block,
  Disposition,
  DispositionError,
  Finding,
  FindingRecord,
  Mark,
  Severity,
  StudioDoc,
  Verdict,
} from "./studio-types";
import { JUSTIFICATION_MAX } from "./studio-types";

/** Most to least serious. Used for sorting and for picking the strongest mark. */
export const SEVERITY_RANK: Record<Severity, number> = {
  critical: 0,
  major: 1,
  minor: 2,
  info: 3,
};

/**
 * The one normalisation the model performs: a non-breaking space becomes an
 * ordinary one.
 *
 * contentEditable emits U+00A0 for a space typed at the end of a run, so
 * without this an analyst who types a word and deletes it leaves text that
 * LOOKS identical to the checked text and compares unequal forever. That would
 * leave the Fixed gate unlocked with no change behind it, which is the exact
 * false claim this module exists to prevent.
 */
export function normalizeText(text: string): string {
  return text.replace(/\u00a0/g, " ");
}

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

// ---------------------------------------------------------------------------
// Splicing
// ---------------------------------------------------------------------------

export interface SpliceResult {
  block: Block;
  /** Offset into the new text: the clamped start plus the replacement length. */
  caret: number;
}

/**
 * Move one mark across a known edit of [s, e) replaced by `rlen` characters.
 *
 * The whole table is one rule: the result is the smallest span containing every
 * character of the mark that survived, and a mark with no survivors is dropped.
 * Cases are mutually exclusive and evaluated in this order.
 */
function remapAcrossSplice(m: Mark, s: number, e: number, rlen: number): Mark | null {
  if (m.end <= m.start) return null; // degenerate input
  const delta = rlen - (e - s);

  // Entirely before, including flush against the start. Same object: a mark the
  // edit did not reach must not churn identity and force a re-render.
  if (m.end <= s) return m;
  // Entirely after, including flush against the end.
  if (m.start >= e) return { ...m, start: m.start + delta, end: m.end + delta };
  // Equal to, or strictly inside, the replaced run: every character it described
  // is gone, so there is nothing left to point at.
  if (s <= m.start && m.end <= e) return null;

  let next: Mark;
  if (m.start < s && m.end <= e) {
    // Straddles the start only: keep the surviving head, stop at the edit.
    next = { ...m, start: m.start, end: s };
  } else if (s <= m.start && e < m.end) {
    // Straddles the end only: survivors are all on the far side, so the mark
    // starts after the replacement rather than swallowing it.
    next = { ...m, start: s + rlen, end: m.end + delta };
  } else {
    // Contains the edit. Survivors on BOTH sides, and a span cannot represent a
    // hole, so it has to swallow the replacement.
    next = { ...m, start: m.start, end: m.end + delta };
  }
  return next.end > next.start ? next : null;
}

/**
 * Replace [start, end) of a block's text and carry its marks across.
 *
 * Total by construction: an inverted span is normalised, an out-of-range one is
 * clamped, and nothing throws. `applyFindings` copies API-supplied spans into
 * marks without validating them, so degenerate input genuinely reaches here.
 *
 * Leaves `original`, `checkedText`, `rows` and `type` alone -- those are the
 * caller's business, and a splice that quietly captured a baseline would make
 * the evidence rule depend on which function you happened to call.
 */
export function spliceBlockText(
  block: Block,
  start: number,
  end: number,
  replacement: string,
): SpliceResult {
  const text = block.text;
  const lo = Math.max(0, Math.min(Math.min(start, end), text.length));
  const hi = Math.max(0, Math.min(Math.max(start, end), text.length));
  const insert = normalizeText(replacement);

  // A splice that changes nothing must not look like an edit: capturing a
  // baseline here would unlock the Fixed gate with an empty diff behind it.
  if (text.slice(lo, hi) === insert) {
    return { block, caret: lo + insert.length };
  }

  const marks: Mark[] = [];
  for (const m of block.marks) {
    const next = remapAcrossSplice(m, lo, hi, insert.length);
    if (next) marks.push(next);
  }

  return {
    block: { ...block, text: text.slice(0, lo) + insert + text.slice(hi), marks },
    caret: lo + insert.length,
  };
}

/**
 * Rewrite marks after a block's text changed by an unknown edit.
 *
 * The edit is localised by common prefix and suffix, which is exact for the
 * single-caret edits contentEditable produces, then handed to the same remap
 * rule a deliberate splice uses. One rule, two entry points.
 */
export function remapMarks(oldText: string, newText: string, marks: Mark[]): Mark[] {
  if (oldText === newText) return marks;
  const region = localise(oldText, newText);
  const rlen = newText.length - (oldText.length - region.end) - region.start;

  const out: Mark[] = [];
  for (const m of marks) {
    const next = remapAcrossSplice(m, region.start, region.end, rlen);
    if (next) out.push(next);
  }
  return out;
}

/**
 * The run of `from` that `to` replaced, by common prefix and suffix. Returned in
 * `from` offsets. A pure insertion yields start === end.
 */
function localise(from: string, to: string): { start: number; end: number } {
  const max = Math.min(from.length, to.length);
  let prefix = 0;
  while (prefix < max && from[prefix] === to[prefix]) prefix += 1;

  let suffix = 0;
  while (suffix < max - prefix && from[from.length - 1 - suffix] === to[to.length - 1 - suffix]) {
    suffix += 1;
  }
  return { start: prefix, end: from.length - suffix };
}

// ---------------------------------------------------------------------------
// Staleness: what the analyst has changed since the check
// ---------------------------------------------------------------------------

/**
 * The region of `checkedText` the analyst's edits replaced, in checkedText
 * offsets. Null when the block was never checked, or has been restored to
 * exactly what the checker read.
 *
 * That second case is the point. A sticky "edited" flag says a document changed
 * when the analyst typed a character and deleted it again, and a fix claim
 * resting on that flag would be a claim about nothing.
 */
export function changedRegion(block: Block | undefined): { start: number; end: number } | null {
  if (!block || block.checkedText === undefined) return null;
  if (block.checkedText === block.text) return null;
  return localise(block.checkedText, block.text);
}

/** True when the analyst has changed this block since the check read it. */
export function blockChanged(block: Block | undefined): boolean {
  return changedRegion(block) !== null;
}

/**
 * Has the analyst edited the text THIS finding points at?
 *
 * Span-scoped, not block-scoped: a change to the assay limit in a paragraph
 * says nothing about a finding two sentences later in the same paragraph, and
 * treating it as evidence would let any keystroke close any finding in the
 * block.
 *
 * The touch test is inclusive at both ends so an insertion flush to a boundary
 * counts -- appending an approver immediately after "Version: 5.0" is exactly
 * how that finding gets fixed, and it inserts at the span's end.
 *
 * This single predicate carries both meanings, and they are the same fact: the
 * checker's claim no longer describes the text, and there is now evidence that
 * a fix was attempted here.
 */
export function isStale(block: Block | undefined, f: Finding): boolean {
  if (!block || block.type === "table") return false;
  const region = changedRegion(block);
  if (!region) return false;
  return region.start <= f.end && region.end >= f.start;
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

/** The judgement currently standing on a finding, or undefined when it is open. */
export function currentRecord(f: Finding): FindingRecord | undefined {
  return f.records && f.records.length > 0 ? f.records[f.records.length - 1] : undefined;
}

/**
 * True when a reviewer has closed this finding and the checker has not since
 * contradicted them.
 */
export function isDisposed(f: Finding): boolean {
  return currentRecord(f) !== undefined && f.contested !== true;
}

/**
 * The one definition of open: the reviewer still owes a judgement here.
 *
 * Deliberately NOT "not stale". A finding whose text you just edited is the one
 * you are most likely to want next, so navigation and the rail badge must not
 * skip it. The verdict counts stale findings in their own bucket, which is a
 * different question -- whether the claim still stands as a defect.
 */
export function isOpen(f: Finding): boolean {
  return !isDisposed(f);
}

/**
 * A fix record whose evidence has been edited away: recorded fixed, and the
 * block is now byte-identical to what the checker read.
 *
 * The record is never deleted for this. Deleting a reviewer's judgement because
 * the text moved under it would lose the fact that they made one; surfacing the
 * contradiction is the honest response.
 */
export function recordVoid(block: Block | undefined, f: Finding): boolean {
  const record = currentRecord(f);
  if (!record || record.disposition !== "fixed") return false;
  return !isStale(block, f);
}

// ---------------------------------------------------------------------------
// Commit: the single path every document write goes through
// ---------------------------------------------------------------------------

/**
 * Swap one block into the document and re-derive everything that hangs off it.
 *
 * Every write lands here so staleness and check state can never be set by hand
 * in one place and forgotten in another. Finding objects keep their identity
 * when their staleness did not change, so React sees a stable list.
 */
function commitBlock(doc: StudioDoc, next: Block): StudioDoc {
  const blocks = doc.blocks.map((b) => (b.id === next.id ? next : b));
  const byId = new Map(blocks.map((b) => [b.id, b]));

  const findings = doc.findings.map((f) => {
    const stale = isStale(byId.get(f.blockId), f);
    return stale === (f.stale ?? false) ? f : { ...f, stale };
  });

  // Any edit anywhere invalidates the check, including one in a block that
  // carries no findings: the checker read the whole document, not a subset.
  // "unchecked" and "checking" are not ours to overwrite.
  let checkState = doc.checkState;
  if (checkState === "checked" || checkState === "stale") {
    checkState = blocks.some((b) => blockChanged(b)) ? "stale" : "checked";
  }

  return { ...doc, blocks, findings, checkState };
}

/**
 * Replace one block's text, carrying its marks and re-deriving staleness.
 *
 * Marks of every kind are remapped now, not just highlights. Dropping every
 * finding mark in a block on any keystroke -- which is what this used to do --
 * erased the in-document highlight of findings the edit never touched.
 */
export function applyEdit(doc: StudioDoc, blockId: string, rawText: string): StudioDoc {
  const block = doc.blocks.find((b) => b.id === blockId);
  // A table renders from `rows` and never from `text`, so accepting an edit here
  // would change nothing on screen while still capturing a baseline -- an
  // invisible edit that unlocks the Fixed gate.
  if (!block || block.type === "table") return doc;

  const nextText = normalizeText(rawText);
  if (block.text === nextText) return doc;

  return commitBlock(doc, {
    ...block,
    // Capture the released text once, on the first edit: tracked changes must
    // diff against the version the analyst opened, not the last keystroke.
    original: block.original ?? block.text,
    text: nextText,
    marks: remapMarks(block.text, nextText, block.marks),
  });
}

/**
 * Restore a block to exactly what the checker read.
 *
 * This is the whole of undo. A suggested fix is only offered while the block is
 * unchanged, so the pre-apply text is always `checkedText` and there is no
 * snapshot to keep. A separate undo stack could disagree with the model; this
 * cannot.
 */
export function revertBlock(doc: StudioDoc, blockId: string): StudioDoc {
  const block = doc.blocks.find((b) => b.id === blockId);
  if (!block || block.checkedText === undefined || block.checkedText === block.text) return doc;
  return commitBlock(doc, {
    ...block,
    text: block.checkedText,
    marks: remapMarks(block.text, block.checkedText, block.marks),
  });
}

// ---------------------------------------------------------------------------
// Suggested fixes
// ---------------------------------------------------------------------------

/**
 * Whether a suggested fix can still be applied, and why not when it cannot.
 *
 * The anchor check is what stops a second Apply from doubling the text: after
 * the first one the span no longer holds the excerpt the checker quoted, so the
 * offsets are no longer a description of anything.
 */
export function canApplySuggestion(
  doc: StudioDoc,
  f: Finding,
): { ok: true } | { ok: false; reason: ApplyError } {
  const block = doc.blocks.find((b) => b.id === f.blockId);
  if (!block) return { ok: false, reason: "unknown_finding" };
  if (block.type === "table") return { ok: false, reason: "not_editable" };
  if (f.suggestion === undefined) return { ok: false, reason: "no_suggestion" };
  if (blockChanged(block)) return { ok: false, reason: "anchor_moved" };
  if (block.text.slice(f.start, f.end) !== f.excerpt) return { ok: false, reason: "anchor_moved" };
  return { ok: true };
}

/**
 * Write a finding's suggested fix into the document.
 *
 * Returns the caret the canvas should restore, and a reason on refusal. It does
 * not return the input document silently: a suggested fix that appears to do
 * nothing is how a reviewer stops trusting the tool.
 */
export function applySuggestion(
  doc: StudioDoc,
  findingId: string,
): { doc: StudioDoc; caret: { blockId: string; offset: number } | null; error: ApplyError | null } {
  const f = doc.findings.find((x) => x.id === findingId);
  if (!f) return { doc, caret: null, error: "unknown_finding" };

  const check = canApplySuggestion(doc, f);
  if (!check.ok) return { doc, caret: null, error: check.reason };

  const block = doc.blocks.find((b) => b.id === f.blockId);
  if (!block) return { doc, caret: null, error: "unknown_finding" };

  const { block: spliced, caret } = spliceBlockText(block, f.start, f.end, f.suggestion ?? "");
  if (spliced === block) return { doc, caret: null, error: "anchor_moved" };

  return {
    doc: commitBlock(doc, { ...spliced, original: block.original ?? block.text }),
    caret: { blockId: block.id, offset: caret },
    error: null,
  };
}

// ---------------------------------------------------------------------------
// Dispositions
// ---------------------------------------------------------------------------

/** Which dispositions demand a written justification. "fixed" carries a diff instead. */
function needsJustification(d: Disposition): boolean {
  return d !== "fixed";
}

/**
 * Whether a disposition can be recorded, and why not when it cannot.
 *
 * The "fixed" gate is the important one: it is refused until the analyst has
 * changed the text this finding points at. A reviewer must not be able to
 * assert a fix that did not happen, and the record is worthless if they can.
 * The escape hatch is "fixed elsewhere", which demands words instead of a diff.
 */
export function canDispose(
  doc: StudioDoc,
  f: Finding | undefined,
  disposition: Disposition,
  justification: string,
): { ok: true } | { ok: false; reason: DispositionError } {
  if (!f) return { ok: false, reason: "unknown_finding" };
  if (doc.checkState === "checking") return { ok: false, reason: "check_in_flight" };

  if (disposition === "fixed") {
    const block = doc.blocks.find((b) => b.id === f.blockId);
    if (!block) return { ok: false, reason: "unknown_finding" };
    if (block.type === "table") return { ok: false, reason: "not_editable" };
    if (!isStale(block, f)) return { ok: false, reason: "fix_not_evidenced" };
  }

  if (needsJustification(disposition)) {
    const trimmed = normalizeText(justification).trim();
    if (trimmed.length === 0) return { ok: false, reason: "justification_required" };
    if (trimmed.length > JUSTIFICATION_MAX) return { ok: false, reason: "justification_too_long" };
  }
  return { ok: true };
}

/**
 * Record a judgement on a finding.
 *
 * Append-only. Changing your mind writes a second entry rather than editing the
 * first, so the record can always answer "what did they decide, and did they
 * decide something else before".
 *
 * `by` and `at` are arguments and are stored verbatim. This module has no clock.
 */
export function disposeFinding(
  doc: StudioDoc,
  findingId: string,
  disposition: Disposition,
  justification: string,
  by: string,
  at: string,
): { doc: StudioDoc; error: DispositionError | null } {
  const f = doc.findings.find((x) => x.id === findingId);
  const check = canDispose(doc, f, disposition, justification);
  if (!check.ok) return { doc, error: check.reason };
  if (!f) return { doc, error: "unknown_finding" };

  const record: FindingRecord = {
    disposition,
    justification: needsJustification(disposition) ? normalizeText(justification).trim() : "",
    at,
    by,
    excerpt: f.excerpt,
  };

  return {
    doc: {
      ...doc,
      findings: doc.findings.map((x) =>
        x.id === findingId
          ? // Recording a fresh judgement answers the checker's challenge, so the
            // contested flag clears; the record that was contested stays in the list.
            { ...x, records: [...(x.records ?? []), record], contested: false }
          : x,
      ),
    },
    error: null,
  };
}

// ---------------------------------------------------------------------------
// Marks, highlights, tracked changes
// ---------------------------------------------------------------------------

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
  const region = localise(original, block.text);
  const end = block.text.length - (original.length - region.end);
  return end > region.start ? [{ start: region.start, end, kind: "insert" }] : [];
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

// ---------------------------------------------------------------------------
// Landing a check
// ---------------------------------------------------------------------------

/**
 * Attach an incoming set of findings to the document.
 *
 * Three things beyond the obvious, all of them about not losing a reviewer's
 * work when the checker runs again:
 *
 *   1. Records are carried forward by finding id. A re-check that wiped the
 *      dispositions would destroy the reason the document is in the state it is.
 *   2. A finding that comes back still reporting after it was recorded FIXED is
 *      marked contested and re-opens, keeping the record visible. Dropping
 *      either side of that contradiction is the audit lie.
 *   3. A recorded finding the checker no longer reports moves to doc.closed.
 *      The defect is gone; the judgement that closed it is still printable.
 *
 * Every block's checkedText is refreshed, not only the ones carrying findings:
 * the checker read the whole document, so the whole document is the baseline.
 */
export function applyFindings(doc: StudioDoc, incoming: Finding[]): StudioDoc {
  const blocks = doc.blocks.map((b) => ({ ...b, checkedText: b.text }));
  const byId = new Map(blocks.map((b) => [b.id, b]));

  const prior = new Map<string, Finding>();
  for (const f of [...(doc.closed ?? []), ...doc.findings]) prior.set(f.id, f);

  const findings = incoming.map((f) => {
    const block = byId.get(f.blockId);
    // Recompute the excerpt from the text rather than trusting the caller: an
    // API-supplied span must never be able to disagree with the text it quotes.
    const excerpt = block ? block.text.slice(f.start, f.end) : f.excerpt;
    const records = prior.get(f.id)?.records;
    const standing = records && records.length > 0 ? records[records.length - 1] : undefined;
    const contested =
      standing !== undefined &&
      (standing.disposition === "fixed" || standing.disposition === "fixed_elsewhere");
    return { ...f, excerpt, stale: false, ...(records ? { records } : {}), contested };
  });

  const returned = new Set(incoming.map((f) => f.id));
  const closed = [...(doc.closed ?? []), ...doc.findings].filter(
    (f) => !returned.has(f.id) && (f.records?.length ?? 0) > 0,
  );

  const byBlock = new Map<string, Finding[]>();
  for (const f of findings) {
    byBlock.set(f.blockId, [...(byBlock.get(f.blockId) ?? []), f]);
  }

  return {
    ...doc,
    checkState: "checked",
    findings,
    closed,
    blocks: blocks.map((b) => {
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

// ---------------------------------------------------------------------------
// Standing
// ---------------------------------------------------------------------------

/** Findings that still count as defects: open, and not invalidated by an edit. */
function liveFindings(doc: StudioDoc): Finding[] {
  return doc.findings.filter((f) => isOpen(f) && !f.stale);
}

/**
 * Is every actionable finding closed by an actual fix?
 *
 * This, and only this, earns the gold thread on the spine. A document whose
 * findings were all DISPUTED has been argued with, not fixed, and must never
 * wear the same seal as one that was corrected. Vacuously true when the check
 * found nothing actionable, which is the ordinary clean document.
 */
export function isSealed(doc: StudioDoc): boolean {
  if (doc.checkState === "unchecked" || doc.checkState === "checking") return false;
  const byId = new Map(doc.blocks.map((b) => [b.id, b]));
  return doc.findings
    .filter((f) => f.severity !== "info")
    .every((f) => {
      const record = currentRecord(f);
      if (!record || f.contested === true) return false;
      if (record.disposition !== "fixed" && record.disposition !== "fixed_elsewhere") return false;
      return !recordVoid(byId.get(f.blockId), f);
    });
}

/**
 * The document's standing.
 *
 * The five buckets partition doc.findings exactly once each, which is what lets
 * the panel show counts that add up. Stale findings are counted apart and never
 * folded into the defect count: a claim about text that has since changed is not
 * evidence of compliance in either direction.
 */
export function verdictFor(doc: StudioDoc): Verdict {
  if (doc.checkState === "unchecked" || doc.checkState === "checking") {
    return {
      tone: "unchecked",
      label: "Not checked yet",
      blocking: 0,
      toResolve: 0,
      notes: 0,
      disposed: 0,
      stale: 0,
    };
  }

  const disposed = doc.findings.filter((f) => isDisposed(f)).length;
  const stale = doc.findings.filter((f) => isOpen(f) && f.stale === true).length;
  const live = liveFindings(doc);
  const blocking = live.filter((f) => f.severity === "critical").length;
  const toResolve = live.filter((f) => f.severity === "major" || f.severity === "minor").length;
  const notes = live.filter((f) => f.severity === "info").length;

  const counts = { blocking, toResolve, notes, disposed, stale };

  if (blocking > 0) return { tone: "blocking", label: "Not submission-ready", ...counts };
  if (toResolve > 0) return { tone: "review", label: "Resolve before filing", ...counts };
  if (stale > 0) return { tone: "review", label: "Re-check after your edits", ...counts };
  if (isSealed(doc)) {
    return doc.checkState === "stale"
      ? { tone: "clear", label: "All findings fixed - re-check to confirm", ...counts }
      : { tone: "clear", label: "No open findings", ...counts };
  }
  if (disposed > 0) return { tone: "settled", label: "All findings recorded", ...counts };
  return { tone: "clear", label: "No open findings", ...counts };
}

/** Findings sorted for display: still open first, by severity, then document order. */
export function sortFindings(doc: StudioDoc): Finding[] {
  const order = new Map(doc.blocks.map((b, i) => [b.id, i]));
  return [...doc.findings].sort((a, b) => {
    const da = isDisposed(a);
    const db = isDisposed(b);
    if (da !== db) return da ? 1 : -1;
    const rank = SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
    if (rank !== 0) return rank;
    return (order.get(a.blockId) ?? 0) - (order.get(b.blockId) ?? 0);
  });
}

/**
 * The next or previous finding still awaiting a judgement, wrapping once.
 * Null when there are none. Informational findings are observations, not work,
 * so traversal skips them.
 */
export function nextOpenFinding(doc: StudioDoc, fromId: string | null, step: 1 | -1): string | null {
  const ordered = sortFindings(doc).filter((f) => isOpen(f) && f.severity !== "info");
  if (ordered.length === 0) return null;

  const at = fromId === null ? -1 : ordered.findIndex((f) => f.id === fromId);
  if (at < 0) return step === 1 ? ordered[0].id : ordered[ordered.length - 1].id;
  return ordered[(at + step + ordered.length) % ordered.length].id;
}

/**
 * The tri-state glyph a file gets in the tree. Mirrors the White Paper cell
 * vocabulary already shipped in globals.css: solid = settled, hollow ring =
 * nothing recorded, clay ring = needs a human.
 */
export function docGlyph(doc: StudioDoc): "clean" | "findings" | "unchecked" | "checking" | "settled" {
  if (doc.checkState === "checking") return "checking";
  if (doc.checkState === "unchecked") return "unchecked";
  if (doc.findings.some((f) => isOpen(f) && !f.stale && f.severity !== "info")) return "findings";
  if (isSealed(doc)) return "clean";
  if (doc.findings.some((f) => isDisposed(f))) return "settled";
  // Checked, nothing open, nothing recorded: either genuinely clean, or edited
  // since and owed a re-check.
  return doc.checkState === "stale" ? "unchecked" : "clean";
}

// ---------------------------------------------------------------------------
// The record, as something you can paste somewhere
// ---------------------------------------------------------------------------

const RECORD_COLUMNS = [
  "document",
  "version",
  "finding_id",
  "severity",
  "location",
  "standard",
  "excerpt",
  "title",
  "disposition",
  "justification",
  "recorded_at_client",
  "recorded_by",
];

const RECORD_TRAILER =
  "Source: RegWatch Compliance Studio working record. Not a controlled record and not an electronic signature.";

/** Tabs and newlines would break the row this lands in, so free text loses them. */
function cell(value: string): string {
  return value.replace(/[\t\r\n]+/g, " ").trim();
}

/**
 * The dispositions as tab-separated text, for pasting into a comment-resolution
 * log or a QA ticket. One row per finding that carries a standing judgement,
 * including ones the checker has stopped reporting.
 *
 * Superseded records stay out: a reviewer who changed their mind produced one
 * decision, and duplicate rows would break the log this exists to feed. The
 * history stays in the model and on the card.
 */
export function formatRecords(doc: StudioDoc): string {
  const rows = [...doc.findings, ...(doc.closed ?? [])]
    .map((f) => ({ f, record: currentRecord(f) }))
    .filter((x): x is { f: Finding; record: FindingRecord } => x.record !== undefined)
    .map(({ f, record }) =>
      [
        doc.name,
        doc.version,
        f.id,
        f.severity,
        f.location,
        f.standard,
        record.excerpt,
        f.title,
        record.disposition,
        record.justification,
        record.at,
        record.by,
      ]
        .map(cell)
        .join("\t"),
    );

  return [RECORD_COLUMNS.join("\t"), ...rows, RECORD_TRAILER].join("\n");
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
