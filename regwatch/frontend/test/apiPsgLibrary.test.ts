import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchPsgLibrary, psgPdfPath, type PsgLibraryDoc } from "@/lib/api";

// These exercise the REAL client paging loop against a stubbed fetch -- the
// StudioPage suite mocks @/lib/api wholesale, so without this file the loop
// and the iframe path builder would have zero coverage.

function row(id: number): PsgLibraryDoc {
  return {
    id,
    active_ingredient: `Drug ${id}`,
    normalized_name: `drug ${id}`,
    stripped_name: `drug ${id}`,
    dosage_form: "Tablet",
    route: "Oral",
    appl_no: null,
    psg_type: "final",
    recommended_date: null,
    source_url: "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_000001.pdf",
  };
}

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchPsgLibrary", () => {
  it("fetches the whole catalog in one call when it fits the cap", async () => {
    const fetchMock = vi.fn((url: string) => {
      expect(url).toBe("/api/psg/documents?limit=5000&offset=0");
      const docs = [row(1), row(2)];
      return Promise.resolve(
        jsonResponse({ count: 2, total: 2, limit: 5000, offset: 0, documents: docs }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const out = await fetchPsgLibrary();
    expect(out).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("loop-fetches with advancing offsets only when the catalog overflows a page", async () => {
    const first = Array.from({ length: 5000 }, (_, i) => row(i + 1));
    const rest = Array.from({ length: 1001 }, (_, i) => row(5001 + i));
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/psg/documents?limit=5000&offset=0") {
        return Promise.resolve(
          jsonResponse({ count: 5000, total: 6001, limit: 5000, offset: 0, documents: first }),
        );
      }
      expect(url).toBe("/api/psg/documents?limit=5000&offset=5000");
      return Promise.resolve(
        jsonResponse({ count: 1001, total: 6001, limit: 5000, offset: 5000, documents: rest }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const out = await fetchPsgLibrary();
    expect(out).toHaveLength(6001);
    expect(out[0].id).toBe(1);
    expect(out[6000].id).toBe(6001);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stops on an empty page so a lying total cannot loop forever", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse({ count: 0, total: 999999, limit: 5000, offset: 0, documents: [] }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const out = await fetchPsgLibrary();
    expect(out).toHaveLength(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("psgPdfPath", () => {
  it("builds the literal same-origin /api path the iframe needs", () => {
    // Deliberately NOT apiBase(): the iframe navigation must ride the
    // same-origin rewrite so the session cookie attaches in every dev mode.
    expect(psgPdfPath(12)).toBe("/api/psg/documents/12/pdf");
  });
});
