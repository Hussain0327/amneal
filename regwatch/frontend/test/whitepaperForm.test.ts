// The printed-form model behind surface 04: the wire's four sections regrouped
// into the template's seven tables, and the footnote apparatus that replaces
// per-cell evidence disclosure. These are the two places where the on-screen
// document can silently disagree with the .docx it downloads.
import { describe, expect, it } from "vitest";

import type { WhitepaperCell, WhitepaperEvidence, WhitepaperSectionData } from "@/lib/api";
import {
  blankGroups,
  buildRefs,
  displayLabel,
  FORM_TABLES,
  groupCells,
  tallyGroups,
} from "@/lib/whitepaper-form";

function cell(id: string, over: Partial<WhitepaperCell> = {}): WhitepaperCell {
  return {
    id,
    label: id,
    mode: "auto",
    status: "populated",
    value: "v",
    evidence: [],
    note: null,
    ...over,
  };
}

function ev(over: Partial<WhitepaperEvidence> = {}): WhitepaperEvidence {
  return {
    source: "Orange Book",
    locator: "products.txt appl_no=020503",
    source_url: null,
    fetched_at: null,
    page: null,
    section: null,
    snippet: null,
    ...over,
  };
}

// The four sections the API actually sends, in wire order.
function wireSections(): WhitepaperSectionData[] {
  return [
    { title: "Proposed Generic Product", cells: [cell("product_name"), cell("rd_center")] },
    {
      title: "Reference Listed Drug Product",
      // The template prints these under two different tables.
      cells: [cell("proprietary_name"), cell("epc"), cell("dea_classification")],
    },
    {
      title: "Product Specific Bioequivalence Recommendation Guidance",
      cells: [
        cell("be_guidance_available"),
        cell("proposed_strategy", { label: `BE Strategy \u2192 Proposed Strategy` }),
        cell("tablet_scoring", { label: `Required Studies \u2192 Tablet Scoring` }),
      ],
    },
    { title: "Action Items", cells: [cell("prepared_by")] },
  ];
}

describe("groupCells -- the wire's sections regrouped into the printed tables", () => {
  it("splits labeling out of the RLD section and the studies out of the PSG section", () => {
    const groups = groupCells(wireSections());
    const byTitle = Object.fromEntries(groups.map((g) => [g.title, g.rows.map((r) => r.cell.id)]));

    expect(byTitle["Reference Listed Drug Product"]).toEqual(["proprietary_name"]);
    expect(byTitle["Labeling"]).toEqual(["epc", "dea_classification"]);
    expect(byTitle["Product Specific Bioequivalence Recommendation Guidance"]).toEqual([
      "be_guidance_available",
    ]);
    expect(byTitle["BE Strategy"]).toEqual(["proposed_strategy"]);
    expect(byTitle["Required Studies"]).toEqual(["tablet_scoring"]);
  });

  it("prints the tables in the template's order, not the wire's", () => {
    expect(groupCells(wireSections()).map((g) => g.title)).toEqual([
      "Proposed Generic Product",
      "Reference Listed Drug Product",
      "Labeling",
      "Product Specific Bioequivalence Recommendation Guidance",
      "BE Strategy",
      "Required Studies",
      "Action Items",
    ]);
  });

  it("strips the group name the template repeats onto its own cell labels", () => {
    const groups = groupCells(wireSections());
    const studies = groups.find((g) => g.title === "Required Studies");
    expect(studies?.rows[0].label).toBe("Tablet Scoring");
    const strategy = groups.find((g) => g.title === "BE Strategy");
    expect(strategy?.rows[0].label).toBe("Proposed Strategy");
    // A label that does not name the current group is left whole.
    expect(displayLabel(`BE Strategy \u2192 Proposed Strategy`, "Labeling")).toBe(
      `BE Strategy \u2192 Proposed Strategy`,
    );
  });

  it("never drops a cell the template map does not know", () => {
    const sections = wireSections();
    sections[0].cells.push(cell("storage_conditions"));
    sections.push({ title: "Novel section", cells: [cell("brand_new_cell")] });

    const groups = groupCells(sections);
    const ids = groups.flatMap((g) => g.rows.map((r) => r.cell.id));

    // Every wire cell is placed exactly once.
    const wireIds = sections.flatMap((s) => s.cells.map((c) => c.id));
    expect(ids.slice().sort()).toEqual(wireIds.slice().sort());
    // An unmapped cell falls back to a table named after its wire section.
    expect(groups.find((g) => g.title === "Proposed Generic Product")?.rows.at(-1)?.cell.id).toBe(
      "storage_conditions",
    );
    expect(groups.find((g) => g.title === "Novel section")?.rows.map((r) => r.cell.id)).toEqual([
      "brand_new_cell",
    ]);
  });

  it("omits a table the payload has no cells for", () => {
    const groups = groupCells([{ title: "Action Items", cells: [cell("approved_by")] }]);
    expect(groups.map((g) => g.title)).toEqual(["Action Items"]);
  });

  it("covers the whole 46-cell template with no duplicate slots", () => {
    const ids = FORM_TABLES.flatMap((t) => t.cells.map((c) => c.id));
    expect(ids).toHaveLength(46);
    expect(new Set(ids).size).toBe(46);
    // The blank form draws every one of them before a run exists.
    expect(blankGroups().reduce((n, g) => n + g.labels.length, 0)).toBe(46);
  });
});

describe("buildRefs -- the provenance appendix", () => {
  it("numbers each distinct record once and lists every cell that cites it", () => {
    const groups = groupCells([
      {
        title: "Proposed Generic Product",
        cells: [
          cell("product_name", { label: "Product Name", evidence: [ev()] }),
          cell("dosage_form", {
            label: "Dosage Form",
            evidence: [ev(), ev({ source: "DailyMed SPL", locator: "setid=x" })],
          }),
        ],
      },
    ]);
    const { refs, byCell } = buildRefs(groups);

    expect(refs.map((r) => r.n)).toEqual([1, 2]);
    // The appendix names cells by the label the WIRE sent, which is what the
    // .docx provenance table prints too.
    expect(refs[0].citedBy).toEqual(["Product Name", "Dosage Form"]);
    expect(refs[1].citedBy).toEqual(["Dosage Form"]);
    expect(byCell.get("product_name")).toEqual([1]);
    expect(byCell.get("dosage_form")).toEqual([1, 2]);
  });

  it("treats a different page or section of the same source as its own record", () => {
    const groups = groupCells([
      {
        title: "Proposed Generic Product",
        cells: [cell("product_name", { evidence: [ev({ page: 2 }), ev({ page: 4 })] })],
      },
    ]);
    expect(buildRefs(groups).refs).toHaveLength(2);
  });

  it("gives an uncited cell no footnote marker at all", () => {
    const groups = groupCells([
      { title: "Action Items", cells: [cell("prepared_by", { status: "analyst_input_required" })] },
    ]);
    const { refs, byCell } = buildRefs(groups);
    expect(refs).toHaveLength(0);
    expect(byCell.has("prepared_by")).toBe(false);
  });
});

describe("tallyGroups", () => {
  it("counts the three cell states over the regrouped document", () => {
    const groups = groupCells([
      {
        title: "Proposed Generic Product",
        cells: [
          cell("product_name"),
          cell("rd_center", { status: "analyst_input_required" }),
          cell("drug_shortage", { status: "verified_absent" }),
        ],
      },
    ]);
    expect(tallyGroups(groups)).toEqual({ populated: 1, absent: 1, pending: 1, total: 3 });
  });
});
