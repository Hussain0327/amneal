import { describe, expect, it } from "vitest";

import { CHECK_RESULTS, DOCS, TREE } from "@/lib/studio-fixtures";
import {
  addHighlight,
  applyEdit,
  applyFindings,
  applySuggestion,
  blockChanged,
  canApplySuggestion,
  canDispose,
  changeMarks,
  changedRegion,
  clearHighlights,
  currentRecord,
  disposeFinding,
  docGlyph,
  dominantMark,
  formatRecords,
  isDisposed,
  isOpen,
  isSealed,
  isStale,
  nextOpenFinding,
  normalizeText,
  offsetWithin,
  quote,
  recordVoid,
  remapMarks,
  revertBlock,
  segmentBlock,
  selectionOffsets,
  sortFindings,
  spliceBlockText,
  verdictFor,
} from "@/lib/studio-marks";
import type { Block, Finding, Mark, StudioDoc } from "@/lib/studio-types";

function doc(over: Partial<StudioDoc> = {}): StudioDoc {
  return {
    id: "d",
    name: "Spec.docx",
    path: "Module 3",
    version: "1.0",
    standards: ["ICH Q6A"],
    checkState: "unchecked",
    findings: [],
    blocks: [
      { id: "b1", type: "p", text: "Assay 98.0% to 102.0% of label claim.", marks: [] },
      { id: "b2", type: "p", text: "LOD applies below the quantitation limit.", marks: [] },
    ],
    ...over,
  };
}

function finding(over: Partial<Finding> = {}): Finding {
  return {
    id: "f1",
    severity: "major",
    title: "t",
    detail: "d",
    blockId: "b1",
    start: 0,
    end: 5,
    location: "Section 1",
    standard: "ICH Q6A",
    excerpt: "Assay",
    ...over,
  };
}

/**
 * A document that has been checked: every block's checkedText matches its text,
 * which is the state applyFindings leaves behind and the baseline the staleness
 * rule measures against.
 */
function checkedDoc(over: Partial<StudioDoc> = {}): StudioDoc {
  const base = doc(over);
  return {
    ...base,
    checkState: "checked",
    blocks: base.blocks.map((b) => ({ ...b, checkedText: b.text })),
  };
}

describe("segmentBlock", () => {
  it("returns one unmarked run when there are no marks", () => {
    expect(segmentBlock("abcdef", [])).toEqual([{ text: "abcdef", marks: [] }]);
  });

  it("splits at every mark boundary", () => {
    const marks: Mark[] = [{ start: 2, end: 4, kind: "finding", findingId: "f1" }];
    expect(segmentBlock("abcdef", marks).map((s) => s.text)).toEqual(["ab", "cd", "ef"]);
  });

  it("keeps overlapping marks on the run they both cover", () => {
    const marks: Mark[] = [
      { start: 0, end: 4, kind: "finding", findingId: "f1" },
      { start: 2, end: 6, kind: "highlight" },
    ];
    const segs = segmentBlock("abcdef", marks);
    expect(segs.map((s) => s.text)).toEqual(["ab", "cd", "ef"]);
    // The middle run is covered by both; the outer runs by one each.
    expect(segs[0].marks).toHaveLength(1);
    expect(segs[1].marks).toHaveLength(2);
    expect(segs[2].marks).toHaveLength(1);
  });

  it("clamps out-of-range marks and drops empty ones", () => {
    const marks: Mark[] = [
      { start: -5, end: 2, kind: "highlight" },
      { start: 4, end: 4, kind: "highlight" },
      { start: 5, end: 99, kind: "highlight" },
    ];
    const segs = segmentBlock("abcdef", marks);
    expect(segs.map((s) => s.text).join("")).toBe("abcdef");
    expect(segs.find((s) => s.text === "ab")?.marks).toHaveLength(1);
  });

  it("returns nothing for empty text", () => {
    expect(segmentBlock("", [{ start: 0, end: 3, kind: "highlight" }])).toEqual([]);
  });
});

describe("dominantMark", () => {
  const findings = [finding({ id: "crit", severity: "critical" }), finding({ id: "min", severity: "minor" })];

  it("prefers the analyst's highlight over any automated finding", () => {
    const marks: Mark[] = [
      { start: 0, end: 2, kind: "finding", findingId: "crit" },
      { start: 0, end: 2, kind: "highlight" },
    ];
    expect(dominantMark(marks, findings)?.kind).toBe("highlight");
  });

  it("picks the most severe finding when several overlap", () => {
    const marks: Mark[] = [
      { start: 0, end: 2, kind: "finding", findingId: "min" },
      { start: 0, end: 2, kind: "finding", findingId: "crit" },
    ];
    expect(dominantMark(marks, findings)?.findingId).toBe("crit");
  });

  it("returns null for an unmarked run", () => {
    expect(dominantMark([], findings)).toBeNull();
  });
});

describe("remapMarks", () => {
  const mark = (start: number, end: number): Mark => ({ start, end, kind: "highlight" });

  it("leaves a mark that sits entirely before the edit", () => {
    // "abc|XYZdef" -> insert at index 3
    expect(remapMarks("abcdef", "abcXYZdef", [mark(0, 3)])).toEqual([mark(0, 3)]);
  });

  it("shifts a mark that sits entirely after the edit", () => {
    expect(remapMarks("abcdef", "abcXYZdef", [mark(3, 6)])).toEqual([mark(6, 9)]);
  });

  it("keeps the surviving tail of a mark the edit cut into", () => {
    // "cd" -> "Z" inside a mark covering "cde". The "e" survived and is now at
    // index 3, so the mark narrows onto it. This is not a guess: the splice
    // knows exactly which characters lived, and a finding the analyst is
    // halfway through fixing keeps its place in the document instead of
    // vanishing out from under them.
    expect(remapMarks("abcdef", "abZef", [mark(2, 5)])).toEqual([mark(3, 4)]);
  });

  it("drops a mark with no surviving characters", () => {
    // The mark covered exactly the run that was replaced.
    expect(remapMarks("abcdef", "abZef", [mark(2, 4)])).toEqual([]);
  });

  it("is a no-op when the text did not change", () => {
    const marks = [mark(1, 3)];
    expect(remapMarks("abcdef", "abcdef", marks)).toBe(marks);
  });
});

describe("applyEdit", () => {
  const checked = applyFindings(doc(), [
    finding({ id: "fa", blockId: "b1", start: 0, end: 5 }),
    finding({ id: "fb", blockId: "b2", start: 0, end: 3 }),
  ]);

  it("stales only the findings whose own span the edit touched", () => {
    // "98.0%" -> "97.0%" changes index 7. Finding fa points at "Assay", [0,5).
    // An edit two words away says nothing about whether that word is right, so
    // it must not become evidence that somebody fixed it.
    const next = applyEdit(checked, "b1", "Assay 97.0% to 102.0% of label claim.");
    expect(next.findings.find((f) => f.id === "fa")?.stale).toBe(false);
    expect(next.findings.find((f) => f.id === "fb")?.stale).toBe(false);

    const onSpan = applyEdit(checked, "b1", "Value 98.0% to 102.0% of label claim.");
    expect(onSpan.findings.find((f) => f.id === "fa")?.stale).toBe(true);
    expect(onSpan.findings.find((f) => f.id === "fb")?.stale).toBe(false);
  });

  it("clears staleness when the analyst types the text back", () => {
    const edited = applyEdit(checked, "b1", "Value 98.0% to 102.0% of label claim.");
    expect(edited.findings.find((f) => f.id === "fa")?.stale).toBe(true);
    const restored = applyEdit(edited, "b1", checked.blocks[0].text);
    expect(restored.findings.find((f) => f.id === "fa")?.stale).toBe(false);
    expect(restored.checkState).toBe("checked");
  });

  it("moves the document to stale so the UI stops presenting the check as current", () => {
    expect(applyEdit(checked, "b1", "changed").checkState).toBe("stale");
  });

  it("moves the document to stale even when the edited block carries no findings", () => {
    const three = applyFindings(doc(), [finding({ id: "fa", blockId: "b1", start: 0, end: 5 })]);
    expect(applyEdit(three, "b2", "rewritten").checkState).toBe("stale");
  });

  it("keeps the finding marks the edit did not cut into, and keeps highlights", () => {
    const withMark = addHighlight(checked, "b1", 20, 30);
    const next = applyEdit(withMark, "b1", `${withMark.blocks[0].text} Added.`);
    const marks = next.blocks[0].marks;
    // The append touches neither the finding span nor the highlight, so both live.
    expect(marks).toHaveLength(2);
    expect(marks.filter((m) => m.kind === "finding")).toEqual([
      { start: 0, end: 5, kind: "finding", findingId: "fa" },
    ]);
  });

  it("refuses to edit a table block, so an invisible edit cannot unlock anything", () => {
    const withTable = checkedDoc({
      blocks: [{ id: "t1", type: "table", text: "Acceptance criteria table", marks: [], rows: [] }],
    });
    const next = applyEdit(withTable, "t1", "tampered");
    expect(next).toBe(withTable);
    expect(next.blocks[0].original).toBeUndefined();
  });

  it("returns the same object when nothing changed", () => {
    expect(applyEdit(checked, "b1", checked.blocks[0].text)).toBe(checked);
    expect(applyEdit(checked, "nope", "x")).toBe(checked);
  });
});

describe("changeMarks", () => {
  it("marks nothing on a block the analyst has not touched", () => {
    expect(changeMarks(doc().blocks[0])).toEqual([]);
  });

  /** The marked run, read back out of the block it points at. */
  function marked(block: { text: string; original?: string; marks: Mark[] }): string[] {
    return changeMarks(block as never).map((m) => block.text.slice(m.start, m.end));
  }

  it("narrows the mark to the characters that actually changed", () => {
    // "98.0%" -> "97.0%": only the one digit moved, so only it is marked.
    const once = applyEdit(doc(), "b1", "Assay 97.0% to 102.0% of label claim.");
    expect(changeMarks(once.blocks[0])).toEqual([{ start: 7, end: 8, kind: "insert" }]);
    expect(marked(once.blocks[0])).toEqual(["7"]);
  });

  it("keeps diffing against the released text, not the previous keystroke", () => {
    const once = applyEdit(doc(), "b1", "Assay 97.0% to 102.0% of label claim.");
    const twice = applyEdit(once, "b1", "Assay 96.0% to 102.0% of label claim.");
    expect(twice.blocks[0].original).toBe(doc().blocks[0].text);
    expect(marked(twice.blocks[0])).toEqual(["6"]);
  });

  it("collapses several edits into the single run that contains them", () => {
    // Two separate digits changed; the mark is the smallest span covering both.
    const edited = applyEdit(doc(), "b1", "Assay 97.0% to 103.0% of label claim.");
    expect(marked(edited.blocks[0])).toEqual(["7.0% to 103"]);
  });

  it("marks nothing for a pure deletion, since no text remains to underline", () => {
    const edited = applyEdit(doc(), "b1", "Assay of label claim.");
    expect(changeMarks(edited.blocks[0])).toEqual([]);
  });

  it("returns to no marks when the text is typed back to the original", () => {
    const original = doc().blocks[0].text;
    const edited = applyEdit(doc(), "b1", "Assay 97.0% to 102.0% of label claim.");
    const restored = applyEdit(edited, "b1", original);
    expect(changeMarks(restored.blocks[0])).toEqual([]);
  });
});

describe("addHighlight / clearHighlights", () => {
  it("merges a new highlight into one it touches", () => {
    const once = addHighlight(doc(), "b1", 0, 5);
    const twice = addHighlight(once, "b1", 4, 10);
    const marks = twice.blocks[0].marks.filter((m) => m.kind === "highlight");
    expect(marks).toEqual([{ start: 0, end: 10, kind: "highlight" }]);
  });

  it("keeps disjoint highlights separate", () => {
    const once = addHighlight(doc(), "b1", 0, 5);
    const twice = addHighlight(once, "b1", 20, 25);
    expect(twice.blocks[0].marks.filter((m) => m.kind === "highlight")).toHaveLength(2);
  });

  it("ignores an empty or inverted span", () => {
    expect(addHighlight(doc(), "b1", 5, 5)).toEqual(doc());
  });

  it("clears highlights without touching finding marks", () => {
    const checked = applyFindings(doc(), [finding({ blockId: "b1", start: 0, end: 5 })]);
    const marked = addHighlight(checked, "b1", 10, 15);
    const cleared = clearHighlights(marked);
    expect(cleared.blocks[0].marks).toHaveLength(1);
    expect(cleared.blocks[0].marks[0].kind).toBe("finding");
  });
});

describe("verdictFor", () => {
  it("reports an unchecked document as unchecked, with no counts", () => {
    expect(verdictFor(doc())).toMatchObject({ tone: "unchecked", blocking: 0, toResolve: 0 });
  });

  it("is blocking when any critical finding is open", () => {
    const d = applyFindings(doc(), [finding({ severity: "critical" }), finding({ id: "f2", severity: "minor" })]);
    expect(verdictFor(d)).toMatchObject({ tone: "blocking", blocking: 1, toResolve: 1 });
  });

  it("is review when there are open findings but none blocking", () => {
    const d = applyFindings(doc(), [finding({ severity: "major" })]);
    expect(verdictFor(d).tone).toBe("review");
  });

  it("is clear when the only findings are informational", () => {
    const d = applyFindings(doc(), [finding({ severity: "info" })]);
    expect(verdictFor(d)).toMatchObject({ tone: "clear", notes: 1 });
  });

  it("excludes stale findings from the defect count and counts them apart", () => {
    const d = applyFindings(doc(), [finding({ severity: "critical", blockId: "b1" })]);
    const edited = applyEdit(d, "b1", "different text entirely");
    const v = verdictFor(edited);
    expect(v.blocking).toBe(0);
    expect(v.stale).toBe(1);
    // Not "clear". The analyst edited under the only finding and recorded
    // nothing, so the document is owed a re-check, and saying it is clear would
    // be a false all-clear on a document nobody has re-read.
    expect(v.tone).toBe("review");
    expect(v.label).toBe("Re-check after your edits");
  });

  it("partitions every finding exactly once across the five buckets", () => {
    const d = applyFindings(doc(), [
      finding({ id: "a", severity: "critical", blockId: "b1", start: 0, end: 5 }),
      finding({ id: "b", severity: "minor", blockId: "b2", start: 0, end: 3 }),
      finding({ id: "c", severity: "info", blockId: "b2", start: 10, end: 14 }),
    ]);
    const edited = applyEdit(d, "b1", "Value 98.0% to 102.0% of label claim.");
    const recorded = disposeFinding(edited, "b", "disputed", "The method states it.", "u", "T").doc;
    const v = verdictFor(recorded);
    expect(v.blocking + v.toResolve + v.notes + v.disposed + v.stale).toBe(recorded.findings.length);
    expect(v).toMatchObject({ blocking: 0, toResolve: 0, notes: 1, disposed: 1, stale: 1 });
  });
});

describe("sortFindings", () => {
  it("orders by severity, then by position in the document", () => {
    const d = applyFindings(doc(), [
      finding({ id: "minor-b2", severity: "minor", blockId: "b2" }),
      finding({ id: "crit-b2", severity: "critical", blockId: "b2" }),
      finding({ id: "crit-b1", severity: "critical", blockId: "b1" }),
    ]);
    expect(sortFindings(d).map((f) => f.id)).toEqual(["crit-b1", "crit-b2", "minor-b2"]);
  });

  it("sinks recorded findings below the ones still owed a judgement", () => {
    // Editing under a finding no longer exiles it: that is the one you are most
    // likely to be working on. Only a recorded judgement moves it out of the way.
    const d = applyFindings(doc(), [
      finding({ id: "crit-b1", severity: "critical", blockId: "b1", start: 0, end: 5 }),
      finding({ id: "minor-b2", severity: "minor", blockId: "b2", start: 0, end: 3 }),
    ]);
    const edited = applyEdit(d, "b1", "Value 98.0% to 102.0% of label claim.");
    expect(sortFindings(edited).map((f) => f.id)).toEqual(["crit-b1", "minor-b2"]);

    const recorded = disposeFinding(edited, "crit-b1", "fixed", "", "u", "T").doc;
    expect(sortFindings(recorded).map((f) => f.id)).toEqual(["minor-b2", "crit-b1"]);
  });
});

describe("docGlyph", () => {
  it("is unchecked before a run and after an edit invalidates one", () => {
    expect(docGlyph(doc())).toBe("unchecked");
    const d = applyFindings(doc(), [finding({ blockId: "b1" })]);
    expect(docGlyph(applyEdit(d, "b1", "x"))).toBe("unchecked");
  });

  it("is clean when a check found nothing that needs fixing", () => {
    expect(docGlyph(applyFindings(doc(), []))).toBe("clean");
    expect(docGlyph(applyFindings(doc(), [finding({ severity: "info" })]))).toBe("clean");
  });

  it("is findings when an actionable finding is open", () => {
    expect(docGlyph(applyFindings(doc(), [finding({ severity: "minor" })]))).toBe("findings");
  });

  it("is checking while a run is in flight", () => {
    expect(docGlyph({ ...doc(), checkState: "checking" })).toBe("checking");
  });
});

describe("DOM offsets", () => {
  function mount(html: string): HTMLElement {
    const el = document.createElement("div");
    el.innerHTML = html;
    document.body.appendChild(el);
    return el;
  }

  it("counts through mark wrappers to a plain-text offset", () => {
    const el = mount('Assay <mark class="st-mark">98.0%</mark> min');
    const target = el.querySelector("mark")?.firstChild as Text;
    expect(offsetWithin(el, target, 4)).toBe(10);
  });

  it("returns a span for a real selection and null for a collapsed one", () => {
    const el = mount("Assay 98.0% min");
    const range = document.createRange();
    const text = el.firstChild as Text;
    range.setStart(text, 6);
    range.setEnd(text, 11);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    expect(selectionOffsets(el, sel)).toEqual({ start: 6, end: 11 });

    range.collapse(true);
    sel?.removeAllRanges();
    sel?.addRange(range);
    expect(selectionOffsets(el, sel)).toBeNull();
  });

  it("ignores a selection outside the given root", () => {
    const a = mount("inside");
    const b = mount("outside");
    const range = document.createRange();
    range.setStart(b.firstChild as Text, 0);
    range.setEnd(b.firstChild as Text, 3);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    expect(selectionOffsets(a, sel)).toBeNull();
  });
});

describe("quote", () => {
  it("collapses whitespace and truncates long passages", () => {
    expect(quote("  a\n\n b  ")).toBe("a b");
    expect(quote("abcdefghij", 5)).toBe("abcd...");
  });
});

describe("fixtures", () => {
  // The fixture module resolves every finding span by searching the block text
  // at import time, so a drifted offset throws on load. These assertions prove
  // the anchors point at the words the finding actually talks about.
  it("anchors every fixture finding to real text in its block", () => {
    for (const [docId, findings] of Object.entries(CHECK_RESULTS)) {
      const d = DOCS.find((x) => x.id === docId);
      expect(d, `no document for check result "${docId}"`).toBeDefined();
      for (const f of findings) {
        const b = d?.blocks.find((x) => x.id === f.blockId);
        expect(b, `finding ${f.id} points at missing block ${f.blockId}`).toBeDefined();
        expect(f.end).toBeGreaterThan(f.start);
        expect(f.end).toBeLessThanOrEqual(b?.text.length ?? 0);
        expect(b?.text.slice(f.start, f.end).trim().length).toBeGreaterThan(0);
      }
    }
  });

  it("gives every document in the tree a matching record, and every document a check result", () => {
    const ids = new Set(DOCS.map((d) => d.id));
    const seen: string[] = [];
    const walk = (nodes: typeof TREE) => {
      for (const n of nodes) {
        if (n.kind === "folder") walk(n.children);
        else seen.push(n.docId);
      }
    };
    walk(TREE);
    expect(seen).toHaveLength(DOCS.length);
    for (const id of seen) expect(ids.has(id)).toBe(true);
    for (const id of ids) expect(CHECK_RESULTS[id]).toBeDefined();
  });

  it("keeps committed copy ASCII so it survives docx and PDF round-trips", () => {
    const text = DOCS.flatMap((d) => [d.name, d.path, ...d.blocks.map((b) => b.text)])
      .concat(Object.values(CHECK_RESULTS).flatMap((fs) => fs.flatMap((f) => [f.title, f.detail, f.standard])))
      .join(" ");
    expect(text).not.toMatch(/[^\x00-\x7F]/);
  });

  it("anchors every suggestion to text that is really there, and never on a table", () => {
    for (const d of DOCS) {
      const byId = new Map(d.blocks.map((b) => [b.id, b]));
      for (const f of CHECK_RESULTS[d.id] ?? []) {
        const block = byId.get(f.blockId);
        expect(block, `${f.id} points at a missing block`).toBeDefined();
        expect(block?.text.slice(f.start, f.end)).toBe(f.excerpt);
        // A suggestion on a read-only block could never be applied.
        if (f.suggestion !== undefined) expect(block?.type).not.toBe("table");
      }
    }
  });
});

// ---------------------------------------------------------------------------
// The disposition loop
// ---------------------------------------------------------------------------

describe("normalizeText", () => {
  it("folds a non-breaking space so a typed-and-deleted edit compares equal", () => {
    expect(normalizeText("a\u00a0b")).toBe("a b");
    // Without this, contentEditable's U+00A0 leaves text that looks identical to
    // the checked text and compares unequal forever, holding the gate open.
    const d = applyFindings(doc(), [finding({ blockId: "b1", start: 0, end: 5 })]);
    const typed = applyEdit(d, "b1", "Assay\u00a098.0% to 102.0% of label claim.");
    expect(blockChanged(typed.blocks[0])).toBe(false);
  });
});

describe("spliceBlockText", () => {
  const sb = (marks: Mark[] = []): Block => ({ id: "s", type: "p", text: "0123456789", marks });
  const m = (start: number, end: number): Mark => ({ start, end, kind: "highlight" });
  /** Replace [3,6) "345" with "xy". delta = -1. */
  const splice = (marks: Mark[]) => spliceBlockText(sb(marks), 3, 6, "xy");

  it("replaces the range and leaves type, rows and the input block alone", () => {
    const before = sb([m(1, 2)]);
    const res = spliceBlockText(before, 3, 6, "xy");
    expect(res.block.text).toBe("012xy6789");
    expect(res.block.type).toBe("p");
    expect(before.text).toBe("0123456789");
    expect(before.marks).toEqual([m(1, 2)]);
  });

  it("returns a mark before the edit by reference, so it cannot churn identity", () => {
    const mark = m(1, 2);
    expect(splice([mark]).block.marks[0]).toBe(mark);
  });

  it("leaves a mark flush to the start of the edit alone", () => {
    expect(splice([m(1, 3)]).block.marks).toEqual([m(1, 3)]);
  });

  it("shifts a mark after the edit, including one flush to its end", () => {
    expect(splice([m(6, 9)]).block.marks).toEqual([m(5, 8)]);
  });

  it("drops a mark equal to or strictly inside the replaced run", () => {
    for (const mark of [m(3, 6), m(4, 5), m(3, 5), m(4, 6)]) {
      expect(splice([mark]).block.marks).toEqual([]);
    }
  });

  it("truncates a mark straddling the start to its surviving head", () => {
    expect(splice([m(1, 5)]).block.marks).toEqual([m(1, 3)]);
  });

  it("moves a mark straddling the end to the far side of the replacement", () => {
    expect(splice([m(4, 9)]).block.marks).toEqual([m(5, 8)]);
    expect(splice([m(3, 9)]).block.marks).toEqual([m(5, 8)]);
  });

  it("widens a mark containing the edit, because a span cannot hold a hole", () => {
    expect(splice([m(0, 9)]).block.marks).toEqual([m(0, 8)]);
  });

  it("does not grow a flush mark on a pure insertion, but does grow a containing one", () => {
    const insert = (marks: Mark[]) => spliceBlockText(sb(marks), 3, 3, "ab");
    expect(insert([m(0, 3)]).block.marks).toEqual([m(0, 3)]);
    expect(insert([m(3, 6)]).block.marks).toEqual([m(5, 8)]);
    expect(insert([m(2, 5)]).block.marks).toEqual([m(2, 7)]);
  });

  it("empties the block on a whole-text deletion and drops every mark", () => {
    const res = spliceBlockText(sb([m(0, 9)]), 0, 10, "");
    expect(res.block.text).toBe("");
    expect(res.block.marks).toEqual([]);
    expect(res.caret).toBe(0);
  });

  it("returns the same block object when the splice changes nothing", () => {
    const before = sb([m(1, 2)]);
    // An empty insertion, and a replacement identical to what it replaces. Either
    // one capturing a baseline would unlock Fixed with an empty diff behind it.
    expect(spliceBlockText(before, 4, 4, "").block).toBe(before);
    expect(spliceBlockText(before, 3, 6, "345").block).toBe(before);
  });

  it("normalises an inverted span and clamps an out-of-range one instead of throwing", () => {
    expect(spliceBlockText(sb(), 6, 3, "xy").block.text).toBe("012xy6789");
    expect(spliceBlockText(sb(), -5, 9999, "z").block.text).toBe("z");
  });

  it("drops a zero-length input mark wherever it sits", () => {
    for (const at of [0, 3, 5, 6, 10]) {
      expect(splice([m(at, at)]).block.marks).toEqual([]);
    }
  });

  it("puts the caret after the replacement for grow, shrink, delete and insert", () => {
    expect(spliceBlockText(sb(), 3, 6, "xy").caret).toBe(5);
    expect(spliceBlockText(sb(), 3, 6, "xyzw").caret).toBe(7);
    expect(spliceBlockText(sb(), 3, 6, "").caret).toBe(3);
    expect(spliceBlockText(sb(), 3, 3, "ab").caret).toBe(5);
  });

  it("normalises a non-breaking space out of the replacement", () => {
    expect(spliceBlockText(sb(), 3, 6, "a\u00a0b").block.text).toBe("012a b6789");
  });
});

describe("changedRegion and isStale", () => {
  const base = () => applyFindings(doc(), [finding({ blockId: "b1", start: 0, end: 5 })]);

  it("reports no region on a block nobody has touched", () => {
    expect(changedRegion(base().blocks[0])).toBeNull();
    expect(changedRegion(undefined)).toBeNull();
  });

  it("reports the region in checkedText offsets", () => {
    const edited = applyEdit(base(), "b1", "Value 98.0% to 102.0% of label claim.");
    expect(changedRegion(edited.blocks[0])).toEqual({ start: 0, end: 5 });
  });

  it("reports no region once the text is typed back to what the checker read", () => {
    const edited = applyEdit(base(), "b1", "Value 98.0% to 102.0% of label claim.");
    const restored = applyEdit(edited, "b1", base().blocks[0].text);
    expect(changedRegion(restored.blocks[0])).toBeNull();
  });

  it("counts an insertion flush to either end of the span as touching it", () => {
    const d = base();
    const f = d.findings[0];
    // "Assay" is [0,5). Appending immediately after the last character is how a
    // missing approver or effective date actually gets added, so offset 5 counts.
    const atEnd = applyEdit(d, "b1", "Assay, 98.0% to 102.0% of label claim.");
    expect(isStale(atEnd.blocks[0], f)).toBe(true);
    const atStart = applyEdit(d, "b1", "Total Assay 98.0% to 102.0% of label claim.");
    expect(isStale(atStart.blocks[0], f)).toBe(true);
  });

  it("does not count an edit that starts one character past the span", () => {
    // "Assay (dried) ..." inserts at offset 6, past the space. That qualifies the
    // NEXT token, not the word the finding points at, and the gap is where the
    // rule earns its keep: an analyst who fixed the problem just outside the
    // span records it as fixed elsewhere and says where, rather than having the
    // tool infer a fix from proximity.
    const near = applyEdit(base(), "b1", "Assay (dried) 98.0% to 102.0% of label claim.");
    expect(changedRegion(near.blocks[0])).toEqual({ start: 6, end: 6 });
    expect(isStale(near.blocks[0], near.findings[0])).toBe(false);
  });

  it("is false for an unchecked block, a table block and a missing block", () => {
    const f = finding({ blockId: "b1", start: 0, end: 5 });
    expect(isStale(doc().blocks[0], f)).toBe(false);
    expect(isStale(undefined, f)).toBe(false);
    const table: Block = { id: "b1", type: "table", text: "x", marks: [], checkedText: "y" };
    expect(isStale(table, f)).toBe(false);
  });
});

describe("applySuggestion and revertBlock", () => {
  const withSuggestion = () =>
    applyFindings(doc(), [
      finding({ id: "s1", blockId: "b1", start: 0, end: 5, suggestion: "Total assay" }),
    ]);

  it("splices the suggestion in, stales the finding and captures the tracked-change baseline", () => {
    const res = applySuggestion(withSuggestion(), "s1");
    expect(res.error).toBeNull();
    expect(res.doc.blocks[0].text).toBe("Total assay 98.0% to 102.0% of label claim.");
    expect(res.doc.blocks[0].original).toBe("Assay 98.0% to 102.0% of label claim.");
    expect(res.doc.findings[0].stale).toBe(true);
    expect(res.doc.checkState).toBe("stale");
    expect(res.caret).toEqual({ blockId: "b1", offset: 11 });
  });

  it("refuses a second apply, so the same fix can never be written twice", () => {
    const once = applySuggestion(withSuggestion(), "s1");
    const twice = applySuggestion(once.doc, "s1");
    expect(twice.error).toBe("anchor_moved");
    expect(twice.doc).toBe(once.doc);
    expect(twice.doc.blocks[0].text).toBe("Total assay 98.0% to 102.0% of label claim.");
  });

  it("refuses after an unrelated edit elsewhere in the same block", () => {
    const moved = applyEdit(withSuggestion(), "b1", "Assay 97.0% to 102.0% of label claim.");
    expect(applySuggestion(moved, "s1").error).toBe("anchor_moved");
  });

  it("refuses an unknown finding, one with no suggestion, and one on a table", () => {
    const d = withSuggestion();
    expect(applySuggestion(d, "nope").error).toBe("unknown_finding");

    const bare = applyFindings(doc(), [finding({ id: "n1", blockId: "b1", start: 0, end: 5 })]);
    expect(applySuggestion(bare, "n1").error).toBe("no_suggestion");

    const tabled = applyFindings(
      doc({ blocks: [{ id: "t1", type: "table", text: "Criteria table", marks: [], rows: [] }] }),
      [finding({ id: "t", blockId: "t1", start: 0, end: 8, suggestion: "x" })],
    );
    expect(applySuggestion(tabled, "t").error).toBe("not_editable");
    expect(tabled.blocks[0].original).toBeUndefined();
  });

  it("restores the checked text and re-locks Fixed", () => {
    const applied = applySuggestion(withSuggestion(), "s1").doc;
    expect(canDispose(applied, applied.findings[0], "fixed", "").ok).toBe(true);

    const reverted = revertBlock(applied, "b1");
    expect(reverted.blocks[0].text).toBe("Assay 98.0% to 102.0% of label claim.");
    expect(reverted.findings[0].stale).toBe(false);
    expect(reverted.checkState).toBe("checked");
    expect(canDispose(reverted, reverted.findings[0], "fixed", "")).toEqual({
      ok: false,
      reason: "fix_not_evidenced",
    });
  });

  it("returns the same document when there is nothing to restore", () => {
    const d = withSuggestion();
    expect(revertBlock(d, "b1")).toBe(d);
    expect(revertBlock(d, "nope")).toBe(d);
  });

  it("never lets Apply and Fixed both be available at once", () => {
    const before = withSuggestion();
    const f = before.findings[0];
    expect(canApplySuggestion(before, f).ok).toBe(true);
    expect(canDispose(before, f, "fixed", "").ok).toBe(false);

    const after = applySuggestion(before, "s1").doc;
    expect(canApplySuggestion(after, after.findings[0]).ok).toBe(false);
    expect(canDispose(after, after.findings[0], "fixed", "").ok).toBe(true);
  });
});

describe("canDispose and disposeFinding", () => {
  const base = () =>
    applyFindings(doc(), [finding({ id: "a", blockId: "b1", start: 0, end: 5 })]);
  const edited = () => applyEdit(base(), "b1", "Value 98.0% to 102.0% of label claim.");

  it("refuses Fixed until the analyst changed the text the finding points at", () => {
    const d = base();
    expect(canDispose(d, d.findings[0], "fixed", "")).toEqual({
      ok: false,
      reason: "fix_not_evidenced",
    });
    const e = edited();
    expect(canDispose(e, e.findings[0], "fixed", "").ok).toBe(true);
  });

  it("refuses Fixed after an edit that missed the span, and after a type-and-undo", () => {
    const elsewhere = applyEdit(base(), "b1", "Assay 97.0% to 102.0% of label claim.");
    expect(canDispose(elsewhere, elsewhere.findings[0], "fixed", "").ok).toBe(false);

    const roundTrip = applyEdit(edited(), "b1", base().blocks[0].text);
    expect(canDispose(roundTrip, roundTrip.findings[0], "fixed", "").ok).toBe(false);
  });

  it("refuses every disposition while a check is in flight", () => {
    const busy = { ...edited(), checkState: "checking" as const };
    expect(canDispose(busy, busy.findings[0], "disputed", "words")).toEqual({
      ok: false,
      reason: "check_in_flight",
    });
  });

  it("requires a justification for everything except Fixed", () => {
    const e = edited();
    for (const d of ["not_applicable", "disputed", "fixed_elsewhere"] as const) {
      expect(canDispose(e, e.findings[0], d, "   ")).toEqual({
        ok: false,
        reason: "justification_required",
      });
      expect(canDispose(e, e.findings[0], d, "a real reason").ok).toBe(true);
    }
    expect(canDispose(e, e.findings[0], "fixed", "").ok).toBe(true);
  });

  it("refuses a justification longer than the maximum", () => {
    const e = edited();
    expect(canDispose(e, e.findings[0], "disputed", "x".repeat(601))).toEqual({
      ok: false,
      reason: "justification_too_long",
    });
  });

  it("stores the injected clock and reviewer verbatim, and never reads a clock itself", () => {
    const res = disposeFinding(edited(), "a", "fixed", "", "A. Analyst", "2026-08-05T10:00:00.000Z");
    const record = currentRecord(res.doc.findings[0]);
    expect(record).toMatchObject({
      disposition: "fixed",
      by: "A. Analyst",
      at: "2026-08-05T10:00:00.000Z",
      justification: "",
      excerpt: "Assay",
    });
  });

  it("appends rather than overwrites when the reviewer changes their mind", () => {
    const first = disposeFinding(edited(), "a", "disputed", "The method allows it.", "u", "T1").doc;
    const second = disposeFinding(first, "a", "fixed", "", "u", "T2").doc;
    expect(second.findings[0].records).toHaveLength(2);
    expect(second.findings[0].records?.[0]).toEqual(first.findings[0].records?.[0]);
    expect(currentRecord(second.findings[0])?.disposition).toBe("fixed");
  });

  it("returns the document untouched and a reason when it refuses", () => {
    const d = base();
    const res = disposeFinding(d, "a", "fixed", "", "u", "T");
    expect(res.error).toBe("fix_not_evidenced");
    expect(res.doc).toBe(d);
    expect(disposeFinding(d, "nope", "fixed", "", "u", "T").error).toBe("unknown_finding");
  });

  it("surfaces a fix record whose evidence was edited away without deleting it", () => {
    const fixed = disposeFinding(edited(), "a", "fixed", "", "u", "T").doc;
    expect(recordVoid(fixed.blocks[0], fixed.findings[0])).toBe(false);

    const reverted = revertBlock(fixed, "b1");
    expect(recordVoid(reverted.blocks[0], reverted.findings[0])).toBe(true);
    expect(reverted.findings[0].records).toHaveLength(1);
    expect(isSealed(reverted)).toBe(false);
  });
});

describe("applyFindings carries the record across a re-check", () => {
  const fixed = () => {
    const d = applyFindings(doc(), [finding({ id: "a", blockId: "b1", start: 0, end: 5 })]);
    const edited = applyEdit(d, "b1", "Value 98.0% to 102.0% of label claim.");
    return disposeFinding(edited, "a", "fixed", "", "u", "T").doc;
  };

  it("sets checkedText on every block, not only the ones carrying findings", () => {
    const d = applyFindings(doc(), [finding({ blockId: "b1" })]);
    expect(d.blocks.map((b) => b.checkedText)).toEqual(d.blocks.map((b) => b.text));
  });

  it("recomputes the excerpt from the text rather than trusting the caller", () => {
    const d = applyFindings(doc(), [finding({ start: 6, end: 11, excerpt: "NONSENSE" })]);
    expect(d.findings[0].excerpt).toBe("98.0%");
  });

  it("keeps the record and contests the finding when the checker still reports it", () => {
    const again = applyFindings(fixed(), [finding({ id: "a", blockId: "b1", start: 0, end: 5 })]);
    expect(again.findings[0].records).toHaveLength(1);
    expect(again.findings[0].contested).toBe(true);
    expect(isOpen(again.findings[0])).toBe(true);
    expect(isDisposed(again.findings[0])).toBe(false);
    expect(again.findings[0].stale).toBe(false);
  });

  it("moves a recorded finding the checker no longer reports into closed", () => {
    const cleared = applyFindings(fixed(), []);
    expect(cleared.findings).toHaveLength(0);
    expect(cleared.closed).toHaveLength(1);
    expect(currentRecord(cleared.closed![0])?.disposition).toBe("fixed");
  });

  it("never loses a record, whichever way the next check comes back", () => {
    const before = fixed();
    for (const next of [[finding({ id: "a", blockId: "b1", start: 0, end: 5 })], []]) {
      const after = applyFindings(before, next);
      const all = [...after.findings, ...(after.closed ?? [])];
      expect(all.filter((f) => (f.records?.length ?? 0) > 0)).toHaveLength(1);
    }
  });

  it("does not contest a finding that was recorded not applicable", () => {
    const d = applyFindings(doc(), [finding({ id: "a", blockId: "b1", start: 0, end: 5 })]);
    const na = disposeFinding(d, "a", "not_applicable", "Out of scope per QA-018.", "u", "T").doc;
    const again = applyFindings(na, [finding({ id: "a", blockId: "b1", start: 0, end: 5 })]);
    expect(again.findings[0].contested).toBe(false);
    expect(isDisposed(again.findings[0])).toBe(true);
  });
});

describe("isSealed", () => {
  const oneFinding = () =>
    applyFindings(doc(), [finding({ id: "a", severity: "major", blockId: "b1", start: 0, end: 5 })]);
  const edited = () => applyEdit(oneFinding(), "b1", "Value 98.0% to 102.0% of label claim.");

  it("is granted when every actionable finding was actually fixed", () => {
    const d = disposeFinding(edited(), "a", "fixed", "", "u", "T").doc;
    expect(isSealed(d)).toBe(true);
    expect(docGlyph(d)).toBe("clean");
    expect(verdictFor(d).tone).toBe("clear");
    expect(verdictFor(d).label).toBe("All findings fixed - re-check to confirm");
  });

  it("is granted for fixed elsewhere, which is a fix that landed somewhere else", () => {
    const d = disposeFinding(
      oneFinding(),
      "a",
      "fixed_elsewhere",
      "Definitions table added before section 1.",
      "u",
      "T",
    ).doc;
    expect(isSealed(d)).toBe(true);
  });

  it("is refused for a document that was argued with rather than corrected", () => {
    for (const disposition of ["disputed", "not_applicable"] as const) {
      const d = disposeFinding(oneFinding(), "a", disposition, "A stated reason.", "u", "T").doc;
      expect(isSealed(d)).toBe(false);
      expect(docGlyph(d)).toBe("settled");
      expect(verdictFor(d)).toMatchObject({ tone: "settled", label: "All findings recorded" });
    }
  });

  it("ignores informational findings, which are observations and not work", () => {
    const d = applyFindings(doc(), [finding({ id: "i", severity: "info" })]);
    expect(isSealed(d)).toBe(true);
  });

  it("is refused while a finding is contested or its fix record is void", () => {
    const d = disposeFinding(edited(), "a", "fixed", "", "u", "T").doc;
    expect(isSealed(applyFindings(d, [finding({ id: "a", blockId: "b1", start: 0, end: 5 })]))).toBe(false);
    expect(isSealed(revertBlock(d, "b1"))).toBe(false);
  });

  it("is refused before a check has ever run", () => {
    expect(isSealed(doc())).toBe(false);
  });
});

describe("nextOpenFinding", () => {
  const three = () =>
    applyFindings(doc(), [
      finding({ id: "a", severity: "critical", blockId: "b1", start: 0, end: 5 }),
      finding({ id: "b", severity: "major", blockId: "b2", start: 0, end: 3 }),
      finding({ id: "c", severity: "info", blockId: "b2", start: 4, end: 11 }),
    ]);

  it("walks the findings still owed a judgement, in panel order, skipping notes", () => {
    const d = three();
    expect(nextOpenFinding(d, null, 1)).toBe("a");
    expect(nextOpenFinding(d, "a", 1)).toBe("b");
    expect(nextOpenFinding(d, "b", 1)).toBe("a");
    expect(nextOpenFinding(d, "a", -1)).toBe("b");
  });

  it("still visits a finding whose text was edited but not yet recorded", () => {
    // This is the one you are most likely to want next, so it must not be skipped.
    const edited = applyEdit(three(), "b1", "Value 98.0% to 102.0% of label claim.");
    expect(nextOpenFinding(edited, "b", 1)).toBe("a");
  });

  it("skips a recorded finding and returns null once nothing is open", () => {
    const d = disposeFinding(three(), "a", "disputed", "Stated reason.", "u", "T").doc;
    expect(nextOpenFinding(d, null, 1)).toBe("b");
    const both = disposeFinding(d, "b", "disputed", "Stated reason.", "u", "T").doc;
    expect(nextOpenFinding(both, null, 1)).toBeNull();
  });
});

describe("formatRecords", () => {
  const recorded = () => {
    const d = applyFindings(doc(), [finding({ id: "a", blockId: "b1", start: 0, end: 5 })]);
    return disposeFinding(d, "a", "disputed", "The method already states it.", "u", "T").doc;
  };

  it("emits a header, one row per standing record, and a provenance trailer", () => {
    const lines = formatRecords(recorded()).split("\n");
    expect(lines[0].split("\t")[0]).toBe("document");
    expect(lines).toHaveLength(3);
    expect(lines[1]).toContain("disputed");
    expect(lines[2]).toContain("Not a controlled record");
  });

  it("gives every row the same column count as the header", () => {
    const lines = formatRecords(recorded()).split("\n");
    const width = lines[0].split("\t").length;
    expect(lines[1].split("\t")).toHaveLength(width);
  });

  it("strips tabs and newlines out of free text so a row cannot break", () => {
    const d = applyFindings(doc(), [finding({ id: "a", blockId: "b1", start: 0, end: 5 })]);
    const messy = disposeFinding(d, "a", "disputed", "one\ttwo\nthree", "u", "T").doc;
    const lines = formatRecords(messy).split("\n");
    expect(lines).toHaveLength(3);
    expect(lines[1]).toContain("one two three");
  });

  it("emits only the current judgement, not the ones it superseded", () => {
    const once = recorded();
    const twice = disposeFinding(once, "a", "not_applicable", "Out of scope.", "u", "T2").doc;
    const lines = formatRecords(twice).split("\n");
    expect(lines).toHaveLength(3);
    expect(lines[1]).toContain("not_applicable");
    expect(lines[1]).not.toContain("disputed");
  });

  it("includes records for findings the checker has stopped reporting", () => {
    const cleared = applyFindings(recorded(), []);
    expect(formatRecords(cleared).split("\n")).toHaveLength(3);
  });

  it("returns a header and trailer for a document with no records at all", () => {
    expect(formatRecords(doc()).split("\n")).toHaveLength(2);
  });
});
