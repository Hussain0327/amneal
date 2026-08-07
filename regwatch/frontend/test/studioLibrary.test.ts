import { describe, expect, it } from "vitest";

import {
  buildLibraryTree,
  countLibraryDocs,
  filterLibrary,
  type PsgWireDoc,
} from "@/lib/studio-library";

function row(over: Partial<PsgWireDoc> & { id: number }): PsgWireDoc {
  return {
    active_ingredient: "Albuterol Sulfate",
    stripped_name: "albuterol",
    dosage_form: "Aerosol, Metered",
    route: "Inhalation",
    psg_type: "final",
    recommended_date: "2024-05-01",
    source_url: "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf",
    ...over,
  };
}

describe("buildLibraryTree", () => {
  it("collapses salt forms of one drug into a single drug folder", () => {
    const tree = buildLibraryTree([
      row({ id: 1, active_ingredient: "Albuterol Sulfate", stripped_name: "albuterol" }),
      row({
        id: 2,
        active_ingredient: "Albuterol",
        stripped_name: "albuterol",
        dosage_form: "Tablet",
        route: "Oral",
      }),
    ]);
    expect(tree).toHaveLength(1);
    expect(tree[0].letter).toBe("A");
    expect(tree[0].drugs).toHaveLength(1);
    expect(tree[0].drugs[0].label).toBe("Albuterol");
    expect(tree[0].drugs[0].docs).toHaveLength(2);
  });

  it("capitalizes each ingredient of a combo key independently", () => {
    const tree = buildLibraryTree([
      row({
        id: 3,
        active_ingredient: "Hydrocodone Bitartrate; Acetaminophen",
        stripped_name: "acetaminophen; hydrocodone",
      }),
    ]);
    expect(tree[0].letter).toBe("A");
    expect(tree[0].drugs[0].label).toBe("Acetaminophen; Hydrocodone");
  });

  it("sorts buckets A-Z with the non-letter bucket last", () => {
    const tree = buildLibraryTree([
      row({ id: 1, stripped_name: "zafirlukast" }),
      row({ id: 2, stripped_name: "17-hydroxyprogesterone" }),
      row({ id: 3, stripped_name: "albuterol" }),
    ]);
    expect(tree.map((b) => b.letter)).toEqual(["A", "Z", "#"]);
    expect(tree[2].id).toBe("lib-b-num");
  });

  it("degrades the doc label through the form/route matrix", () => {
    const [bucket] = buildLibraryTree([
      row({ id: 1, dosage_form: "Tablet", route: "Oral" }),
      row({ id: 2, dosage_form: "Tablet", route: null }),
      row({ id: 3, dosage_form: null, route: "Oral" }),
      row({ id: 4, dosage_form: null, route: null }),
    ]);
    const labels = bucket.drugs[0].docs.map((d) => d.label);
    expect(labels).toContain("Tablet (Oral)");
    expect(labels).toContain("Tablet");
    expect(labels).toContain("Oral");
    expect(labels).toContain("Form not stated");
  });

  it("narrows psg_type so anything unrecognized reads as draft", () => {
    const [bucket] = buildLibraryTree([
      row({ id: 1, psg_type: "final" }),
      row({ id: 2, psg_type: "draft" }),
      row({ id: 3, psg_type: null }),
      row({ id: 4, psg_type: "FINAL" }),
    ]);
    const byId = new Map(bucket.drugs[0].docs.map((d) => [d.psgId, d.psgType]));
    expect(byId.get(1)).toBe("final");
    expect(byId.get(2)).toBe("draft");
    expect(byId.get(3)).toBe("draft");
    expect(byId.get(4)).toBe("draft");
  });

  it("orders docs by label, then final before draft, then id -- input-order independent", () => {
    const docs = [
      row({ id: 30, dosage_form: "Tablet", route: "Oral", psg_type: "draft" }),
      row({ id: 10, dosage_form: "Tablet", route: "Oral", psg_type: "final" }),
      row({ id: 20, dosage_form: "Aerosol", route: "Inhalation", psg_type: "draft" }),
    ];
    const forward = buildLibraryTree(docs);
    const reversed = buildLibraryTree([...docs].reverse());
    const order = (tree: typeof forward) => tree[0].drugs[0].docs.map((d) => d.psgId);
    expect(order(forward)).toEqual([20, 10, 30]);
    expect(order(reversed)).toEqual([20, 10, 30]);
  });

  it("produces stable, unique ids that cannot collide with fixture doc ids", () => {
    const docs = [row({ id: 1 }), row({ id: 2, stripped_name: "budesonide" })];
    const a = buildLibraryTree(docs);
    const b = buildLibraryTree(docs);
    const ids = (tree: typeof a) =>
      tree.flatMap((bucket) => [
        bucket.id,
        ...bucket.drugs.flatMap((drug) => [drug.id, ...drug.docs.map((d) => d.id)]),
      ]);
    expect(ids(a)).toEqual(ids(b));
    expect(new Set(ids(a)).size).toBe(ids(a).length);
    expect(a.flatMap((bucket) => bucket.drugs.flatMap((d) => d.docs.map((doc) => doc.id)))).toEqual(
      expect.arrayContaining(["psg-1", "psg-2"]),
    );
  });

  it("falls back to the lowercased ingredient when stripped_name is missing", () => {
    const tree = buildLibraryTree([
      row({ id: 5, active_ingredient: "Budesonide", stripped_name: null }),
      row({ id: 6, active_ingredient: "Budesonide", stripped_name: "" }),
    ]);
    expect(tree).toHaveLength(1);
    expect(tree[0].drugs).toHaveLength(1);
    expect(tree[0].drugs[0].label).toBe("Budesonide");
  });
});

describe("filterLibrary", () => {
  const tree = buildLibraryTree([
    row({ id: 1, active_ingredient: "Albuterol Sulfate", stripped_name: "albuterol" }),
    row({
      id: 2,
      active_ingredient: "Albuterol",
      stripped_name: "albuterol",
      dosage_form: "Tablet",
      route: "Oral",
    }),
    row({
      id: 3,
      active_ingredient: "Budesonide",
      stripped_name: "budesonide",
      dosage_form: "Suspension",
      route: "Nasal",
    }),
  ]);

  it("keeps all docs of a drug whose label matches", () => {
    const out = filterLibrary(tree, "albuterol");
    expect(out).toHaveLength(1);
    expect(out[0].drugs[0].docs).toHaveLength(2);
  });

  it("keeps only matching docs when only a doc label matches", () => {
    const out = filterLibrary(tree, "suspension");
    expect(out).toHaveLength(1);
    expect(out[0].drugs[0].label).toBe("Budesonide");
    expect(out[0].drugs[0].docs).toHaveLength(1);
  });

  it("finds the salt row by raw ingredient even though the drug label stripped it", () => {
    const out = filterLibrary(tree, "sulfate");
    expect(out).toHaveLength(1);
    expect(out[0].drugs[0].docs.map((d) => d.psgId)).toEqual([1]);
  });

  it("returns empty on no match and everything on an empty needle", () => {
    expect(filterLibrary(tree, "zzzz")).toEqual([]);
    expect(filterLibrary(tree, "")).toHaveLength(tree.length);
  });
});

describe("countLibraryDocs", () => {
  it("counts leaves across buckets", () => {
    const tree = buildLibraryTree([
      row({ id: 1 }),
      row({ id: 2, stripped_name: "budesonide" }),
      row({ id: 3, stripped_name: "zafirlukast" }),
    ]);
    expect(countLibraryDocs(tree)).toBe(3);
    expect(countLibraryDocs([])).toBe(0);
  });
});
