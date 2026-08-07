import { describe, expect, it } from "vitest";

import {
  citationIndex,
  citeKey,
  dedupeCitations,
  pairsIn,
  segmentCitations,
  splitSourcesTrailer,
  trailerMarkerPairs,
} from "@/lib/citations";

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

describe("dedupeCitations", () => {
  it("keeps the first occurrence per (short_name, page) and preserves order", () => {
    const a = { short_name: "PSG_020503", page: 3 };
    const dupA = { short_name: "PSG_020503", page: 3 };
    const b = { short_name: "PSG_021730", page: 4 };
    const out = dedupeCitations([a, dupA, b]);
    expect(out).toEqual([a, b]);
    // Identity, not just equality: the FIRST object survives -- the same rule
    // citationIndex numbers by, so [n] positions stay bijective.
    expect(out[0]).toBe(a);
  });

  it("keys case-insensitively, matching the backend's IGNORECASE parser", () => {
    const upper = { short_name: "PSG_020503", page: 3 };
    const lower = { short_name: "psg_020503", page: 3 };
    expect(dedupeCitations([upper, lower])).toEqual([upper]);
  });

  it("never collapses the same source on different pages", () => {
    expect(
      dedupeCitations([
        { short_name: "PSG_020503", page: 3 },
        { short_name: "PSG_020503", page: 4 },
      ]),
    ).toHaveLength(2);
  });

  it("numbers contiguously through citationIndex after deduping", () => {
    const deduped = dedupeCitations([
      { short_name: "PSG_020503", page: 3 },
      { short_name: "PSG_020503", page: 3 },
      { short_name: "PSG_021730", page: 4 },
    ]);
    const idx = citationIndex(deduped);
    // Without the dedupe the duplicate would leave a hole ([1] then [3]).
    expect(idx.get(citeKey("PSG_020503", 3))).toBe(1);
    expect(idx.get(citeKey("PSG_021730", 4))).toBe(2);
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

describe("splitSourcesTrailer", () => {
  it("splits prose from the trailing Sources bibliography (backend rule mirror)", () => {
    const { prose, trailer } = splitSourcesTrailer(
      "A claim [1].\n\nSources:\n[1] [PSG_020503, p.3]",
    );
    expect(prose).toBe("A claim [1].");
    expect(trailer).toBe("[1] [PSG_020503, p.3]");
  });

  it("returns the whole text as prose when no trailer exists", () => {
    expect(splitSourcesTrailer("Just prose.")).toEqual({ prose: "Just prose.", trailer: null });
  });

  it("never blanks a reply that is ONLY a bibliography", () => {
    const text = "\nSources:\n[1] [PSG_020503, p.3]";
    expect(splitSourcesTrailer(text)).toEqual({ prose: text, trailer: null });
  });

  it("splits at the FIRST trailer marker only", () => {
    const { prose, trailer } = splitSourcesTrailer("A.\nSources:\nB.\nSources:\nC.");
    expect(prose).toBe("A.");
    expect(trailer).toBe("B.\nSources:\nC.");
  });
});

describe("trailerMarkerPairs", () => {
  it("maps bracketed markers to their line's first source pair", () => {
    const map = trailerMarkerPairs("[1] [PSG_020503, p.3]\n[2] [PSG_021457, p.4]");
    expect(map.get(1)).toEqual({ shortName: "PSG_020503", page: 3 });
    expect(map.get(2)).toEqual({ shortName: "PSG_021457", page: 4 });
  });

  it("accepts dotted / unbracketed numbering", () => {
    const map = trailerMarkerPairs("1. PSG_020503, p.3\n2) OB_021730, p.9");
    expect(map.get(1)).toEqual({ shortName: "PSG_020503", page: 3 });
    expect(map.get(2)).toEqual({ shortName: "OB_021730", page: 9 });
  });

  it("skips lines with no source-shaped pair (never fabricates)", () => {
    const map = trailerMarkerPairs("[1] see appendix\n[2] [PSG_020503, p.3]");
    expect(map.has(1)).toBe(false);
    expect(map.get(2)).toEqual({ shortName: "PSG_020503", page: 3 });
  });

  it("keeps the first entry when a marker number repeats", () => {
    const map = trailerMarkerPairs("[1] [PSG_020503, p.3]\n[1] [PSG_021457, p.4]");
    expect(map.get(1)).toEqual({ shortName: "PSG_020503", page: 3 });
    expect(map.size).toBe(1);
  });
});
