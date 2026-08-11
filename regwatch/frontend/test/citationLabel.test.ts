import { describe, expect, it } from "vitest";

import {
  citationLabel,
  citationLabels,
  citationProduct,
  formPhrase,
  revisedMonth,
  type LabelableCitation,
} from "@/lib/citations";

function cite(overrides: Partial<LabelableCitation> = {}): LabelableCitation {
  return {
    short_name: "PSG_020911",
    page: 1,
    product_name: "beclomethasone dipropionate",
    dosage_form: "AEROSOL, METERED",
    route: "INHALATION",
    psg_type: "final",
    recommended_date: "2021-03-15",
    ...overrides,
  };
}

describe("formPhrase", () => {
  it("puts route before form and title-cases both", () => {
    expect(formPhrase("INHALATION", "AEROSOL, METERED")).toBe("Inhalation Aerosol, Metered");
  });

  it("never drops a qualifier to shorten the label", () => {
    // "AEROSOL" and "AEROSOL, METERED" are different products.
    expect(formPhrase(null, "AEROSOL, METERED")).toContain("Metered");
  });

  it("collapses instead of stuttering when one term contains the other", () => {
    expect(formPhrase("ORAL", "TABLET, ORAL")).toBe("Tablet, Oral");
    expect(formPhrase("TRANSDERMAL SYSTEM", "SYSTEM")).toBe("Transdermal System");
  });

  it("returns whichever side is present, or null", () => {
    expect(formPhrase("INHALATION", null)).toBe("Inhalation");
    expect(formPhrase(null, "GEL")).toBe("Gel");
    expect(formPhrase(null, null)).toBeNull();
    expect(formPhrase("  ", "")).toBeNull();
  });
});

describe("revisedMonth", () => {
  it("renders a month and year", () => {
    expect(revisedMonth("2021-03-15")).toBe("Mar 2021");
    expect(revisedMonth("2019-06-01")).toBe("Jun 2019");
  });

  it("is null for anything it cannot parse", () => {
    expect(revisedMonth(null)).toBeNull();
    expect(revisedMonth("")).toBeNull();
    expect(revisedMonth("not a date")).toBeNull();
    expect(revisedMonth("2021-13-01")).toBeNull();
  });
});

describe("citationProduct", () => {
  it("names the ingredient and its form", () => {
    expect(citationProduct(cite())).toBe("Beclomethasone Dipropionate — Inhalation Aerosol, Metered");
  });

  it("is null without a product name, so callers fall back", () => {
    expect(citationProduct(cite({ product_name: null }))).toBeNull();
  });
});

describe("citationLabel", () => {
  it("replaces the application number with something a human can read", () => {
    expect(citationLabel(cite())).toBe(
      "Beclomethasone Dipropionate — Inhalation Aerosol, Metered PSG, revised Mar 2021 · p.1",
    );
  });

  it("omits the revision clause when no date is recorded", () => {
    expect(citationLabel(cite({ recommended_date: null }))).toBe(
      "Beclomethasone Dipropionate — Inhalation Aerosol, Metered PSG · p.1",
    );
  });

  it("falls back whole to short_name for a legacy citation", () => {
    // A turn persisted before identity fields shipped. Inventing an identity
    // would be worse than showing the opaque one.
    expect(citationLabel({ short_name: "PSG_020911", page: 4 })).toBe("PSG_020911 · p.4");
  });
});

describe("citationLabels", () => {
  it("disambiguates two PSGs that collapse to the same label", () => {
    // Exactly audit #1716: one answer cited PSG_020911 and PSG_207921 for the
    // same ingredient and form, which would render as two identical chips.
    const labels = citationLabels([
      cite({ short_name: "PSG_020911" }),
      cite({ short_name: "PSG_207921" }),
    ]);
    expect(labels[0]).toContain("#020911");
    expect(labels[1]).toContain("#207921");
    expect(labels[0]).not.toBe(labels[1]);
  });

  it("leaves an already-unique label alone", () => {
    const labels = citationLabels([
      cite({ short_name: "PSG_020911" }),
      cite({ short_name: "PSG_207921", product_name: "estradiol", recommended_date: "2019-06-01" }),
    ]);
    expect(labels[0]).not.toContain("#");
    expect(labels[1]).not.toContain("#");
  });

  it("disambiguates two legacy citations by page, not by a fake identity", () => {
    const labels = citationLabels([
      { short_name: "PSG_020911", page: 1 },
      { short_name: "PSG_020911", page: 2 },
    ]);
    expect(labels).toEqual(["PSG_020911 · p.1", "PSG_020911 · p.2"]);
  });
});
