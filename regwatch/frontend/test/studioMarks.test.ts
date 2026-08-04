import { describe, expect, it } from "vitest";

import { CHECK_RESULTS, DOCS, TREE } from "@/lib/studio-fixtures";
import {
  addHighlight,
  applyEdit,
  applyFindings,
  changeMarks,
  clearHighlights,
  docGlyph,
  dominantMark,
  offsetWithin,
  quote,
  remapMarks,
  segmentBlock,
  selectionOffsets,
  sortFindings,
  verdictFor,
} from "@/lib/studio-marks";
import type { Finding, Mark, StudioDoc } from "@/lib/studio-types";

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
    ...over,
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

  it("drops a mark the edit cut into rather than guessing a span", () => {
    expect(remapMarks("abcdef", "abZef", [mark(2, 5)])).toEqual([]);
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

  it("stales only the findings anchored in the edited block", () => {
    const next = applyEdit(checked, "b1", "Assay 97.0% to 102.0% of label claim.");
    expect(next.findings.find((f) => f.id === "fa")?.stale).toBe(true);
    expect(next.findings.find((f) => f.id === "fb")?.stale).toBeUndefined();
  });

  it("moves the document to stale so the UI stops presenting the check as current", () => {
    expect(applyEdit(checked, "b1", "changed").checkState).toBe("stale");
  });

  it("drops the finding marks from the edited block but keeps highlights", () => {
    const withMark = addHighlight(checked, "b1", 20, 30);
    const next = applyEdit(withMark, "b1", `${withMark.blocks[0].text} Added.`);
    const marks = next.blocks[0].marks;
    expect(marks.every((m) => m.kind === "highlight")).toBe(true);
    expect(marks).toHaveLength(1);
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

  it("excludes stale findings from the verdict and counts them apart", () => {
    const d = applyFindings(doc(), [finding({ severity: "critical", blockId: "b1" })]);
    const edited = applyEdit(d, "b1", "different text entirely");
    const v = verdictFor(edited);
    expect(v.blocking).toBe(0);
    expect(v.stale).toBe(1);
    expect(v.tone).toBe("clear");
  });
});

describe("sortFindings", () => {
  it("orders by severity, then by position in the document, with stale last", () => {
    const d = applyFindings(doc(), [
      finding({ id: "minor-b2", severity: "minor", blockId: "b2" }),
      finding({ id: "crit-b2", severity: "critical", blockId: "b2" }),
      finding({ id: "crit-b1", severity: "critical", blockId: "b1" }),
    ]);
    const edited = applyEdit(d, "b1", "edited away");
    expect(sortFindings(edited).map((f) => f.id)).toEqual(["crit-b2", "minor-b2", "crit-b1"]);
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
});
