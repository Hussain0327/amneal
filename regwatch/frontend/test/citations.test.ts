import { describe, expect, it } from "vitest";

import { citationIndex, citeKey, pairsIn, segmentCitations } from "@/lib/citations";

// The tokenizer mirrors the backend grammar (src/regwatch/common/citations.py).
// These cases pin the contract the stamp renderer relies on.
describe("pairsIn", () => {
  it("parses a single PSG pair", () => {
    expect(pairsIn("PSG_020503, p.3")).toEqual([{ shortName: "PSG_020503", page: 3 }]);
  });

  it("parses a compound bracket body (semicolon-separated)", () => {
    expect(pairsIn("PSG_020503, p.4; OB_021730, p.4")).toEqual([
      { shortName: "PSG_020503", page: 4 },
      { shortName: "OB_021730", page: 4 },
    ]);
  });

  it("tolerates loose whitespace and bare-digit short names", () => {
    expect(pairsIn("020503 ,  p. 12")).toEqual([{ shortName: "020503", page: 12 }]);
  });

  it("does not treat prose like 'Table 1, p.3' as a citation", () => {
    // "Table" is not a source-shaped token; the >=3-digit rule rejects it.
    expect(pairsIn("Table 1, p.3")).toEqual([]);
  });
});

describe("segmentCitations", () => {
  it("splits prose around a citation bracket", () => {
    const segs = segmentCitations("Bioequivalence is required [PSG_020503, p.3] for this drug.");
    expect(segs).toEqual([
      { kind: "text", value: "Bioequivalence is required " },
      { kind: "cite", raw: "[PSG_020503, p.3]", pairs: [{ shortName: "PSG_020503", page: 3 }] },
      { kind: "text", value: " for this drug." },
    ]);
  });

  it("leaves a non-citation bracket as plain prose", () => {
    const segs = segmentCitations("see [appendix] for detail");
    expect(segs).toEqual([{ kind: "text", value: "see [appendix] for detail" }]);
  });

  it("keeps a compound bracket as one segment with both pairs", () => {
    const segs = segmentCitations("[PSG_1, p.1; PSG_222, p.2]");
    expect(segs).toEqual([
      {
        kind: "cite",
        raw: "[PSG_1, p.1; PSG_222, p.2]",
        // PSG_1 has <3 trailing digits and is dropped by the grammar; PSG_222 kept.
        pairs: [{ shortName: "PSG_222", page: 2 }],
      },
    ]);
  });
});

describe("citationIndex / citeKey", () => {
  it("maps first occurrence to a 1-based index, case-insensitively", () => {
    const idx = citationIndex([
      { short_name: "PSG_020503", page: 3 },
      { short_name: "PSG_021730", page: 4 },
    ]);
    expect(idx.get(citeKey("psg_020503", 3))).toBe(1);
    expect(idx.get(citeKey("PSG_021730", 4))).toBe(2);
    expect(idx.get(citeKey("PSG_999999", 1))).toBeUndefined();
  });
});
