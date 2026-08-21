import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchStructures, type ChemistryStructure } from "@/lib/api";

// fetchStructures is a thin GET wrapper (like fetchPsgLibrary's cousin
// listPsgDocuments), so it gets the same stubbed-fetch treatment as
// apiPsgLibrary.test.ts: the shared getJSON/handle() plumbing (auth gate,
// timeout, non-JSON-body handling) is already covered generically in
// apiErrors.test.ts / apiTimeout.test.ts, so this file only pins what is
// specific to this endpoint -- the URL it hits and the shape it returns.

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

function structure(): ChemistryStructure {
  return {
    name: "albuterol sulfate",
    pubchem_cid: 39859,
    smiles: "CC(C)(C)NCC(C1=CC(=C(C=C1)O)CO)O",
    inchikey: "BNPSSFBVHCLLOS-UHFFFAOYSA-N",
    molecular_formula: "C26H44N2O10S",
    molecular_weight: 576.7,
    iupac_name: null,
    unii: "021SEF3731",
    match: "exact",
    source_url: "https://pubchem.ncbi.nlm.nih.gov/compound/39859",
    fetched_at: "2026-08-21T14:00:00Z",
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchStructures", () => {
  it("hits /chemistry/structures with the ingredient URL-encoded and returns the structures array", async () => {
    const fetchMock = vi.fn((url: string) => {
      expect(url).toBe("/api/chemistry/structures?ingredient=albuterol%20sulfate");
      return Promise.resolve(
        jsonResponse({ ingredient: "albuterol sulfate", structures: [structure()] }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const out = await fetchStructures("albuterol sulfate");
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ pubchem_cid: 39859, match: "exact" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("encodes characters that are meaningful in a query string (e.g. '; ' joining multi-ingredient products)", async () => {
    const fetchMock = vi.fn((url: string) => {
      expect(url).toBe(
        "/api/chemistry/structures?ingredient=amlodipine%3B%20valsartan",
      );
      return Promise.resolve(
        jsonResponse({ ingredient: "amlodipine; valsartan", structures: [] }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await fetchStructures("amlodipine; valsartan");
  });

  it("returns an empty list when nothing is stored for the ingredient", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ ingredient: "an unstored drug", structures: [] })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const out = await fetchStructures("an unstored drug");
    expect(out).toEqual([]);
  });
});
