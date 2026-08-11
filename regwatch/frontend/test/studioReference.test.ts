import { describe, expect, it } from "vitest";

import type { PsgDocumentContent } from "@/lib/api";
import { formLabel, referenceDocId, toReferenceDoc } from "@/lib/studio-reference";

function content(over: Partial<PsgDocumentContent> = {}): PsgDocumentContent {
  return {
    id: 12,
    appl_no: "020503",
    file_name: "PSG_020503 Albuterol Sulfate.docx",
    active_ingredient: "Albuterol Sulfate",
    dosage_form: "Aerosol, Metered",
    route: "Inhalation",
    psg_type: "draft",
    recommended_date: "2024-05-01",
    source_url: "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf",
    page_count: 2,
    truncated: false,
    blocks: [
      { id: "psg-12-b0", type: "title", text: "Guidance on Albuterol Sulfate", page: 1 },
      { id: "psg-12-b1", type: "p", text: "Active Ingredient: Albuterol sulfate", page: 1 },
    ],
    ...over,
  };
}

describe("toReferenceDoc", () => {
  it("maps the wire document onto the studio's own document shape", () => {
    const doc = toReferenceDoc(content());

    expect(doc.id).toBe("psg-12");
    expect(doc.name).toBe("PSG_020503 Albuterol Sulfate.docx");
    expect(doc.path).toBe("Reference library");
    expect(doc.blocks.map((b) => [b.type, b.text])).toEqual([
      ["title", "Guidance on Albuterol Sulfate"],
      ["p", "Active Ingredient: Albuterol sulfate"],
    ]);
    // Ids come from the server so a block keeps one identity end to end.
    expect(doc.blocks[0].id).toBe("psg-12-b0");
  });

  it("carries no compliance state, because nothing has checked it", () => {
    const doc = toReferenceDoc(content());

    expect(doc.findings).toEqual([]);
    expect(doc.checkState).toBe("unchecked");
    expect(doc.standards).toEqual([]);
    // No marks either: a fresh document has nothing highlighted.
    expect(doc.blocks.every((b) => b.marks.length === 0)).toBe(true);
  });

  it("carries FDA's date as the version, and says so when there is none", () => {
    expect(toReferenceDoc(content()).version).toBe("2024-05-01");
    expect(toReferenceDoc(content({ recommended_date: null })).version).toBe("not dated");
  });

  it("degrades an unrecognised block type to a paragraph", () => {
    // The generated wire type is a union today; a server that grew a new block
    // type must render as prose rather than as an unstyled unknown.
    const rogue = content({
      blocks: [
        { id: "psg-12-b0", type: "table" as unknown as "p", text: "cells", page: 1 },
      ],
    });

    expect(toReferenceDoc(rogue).blocks[0].type).toBe("p");
  });

  it("keeps the document id in step with the rail's row id", () => {
    expect(referenceDocId(12)).toBe("psg-12");
    expect(toReferenceDoc(content()).id).toBe(referenceDocId(12));
  });
});

describe("formLabel", () => {
  it("joins form and route, and degrades to whichever side exists", () => {
    expect(formLabel(content())).toBe("Aerosol, Metered (Inhalation)");
    expect(formLabel(content({ route: null }))).toBe("Aerosol, Metered");
    expect(formLabel(content({ dosage_form: null }))).toBe("Inhalation");
    expect(formLabel(content({ dosage_form: null, route: null }))).toBe("Form not stated");
  });
});
