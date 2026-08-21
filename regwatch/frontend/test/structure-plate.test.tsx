import { act, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Intercept only fetchStructures; everything else on @/lib/api stays real
// (same partial-mock pattern turns.test.tsx uses for sendFeedback).
const fetchStructuresMock = vi.fn<(ingredient: string, signal?: AbortSignal) => Promise<unknown>>();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchStructures: (...args: Parameters<typeof fetchStructuresMock>) => fetchStructuresMock(...args),
  };
});

// The library touches `document` at draw time; stub it so the effect's
// dynamic import resolves to a fake drawer that just proves it was called
// (appends an <svg> to the target) rather than pulling in real chemistry.
vi.mock("smiles-drawer", () => {
  class FakeSvgDrawer {
    constructor(_options: unknown) {}
    draw(_data: unknown, target: SVGSVGElement): void {
      target.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "svg"));
    }
  }
  return {
    default: {
      SvgDrawer: FakeSvgDrawer,
      parse: (_smiles: string, onSuccess: (tree: unknown) => void) => onSuccess({}),
    },
  };
});

import { StructurePlate } from "@/components/StructurePlate";
import type { ChemistryStructure } from "@/lib/api";

function structure(overrides: Partial<ChemistryStructure> = {}): ChemistryStructure {
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
    ...overrides,
  };
}

afterEach(() => {
  fetchStructuresMock.mockReset();
});

describe("StructurePlate", () => {
  it("renders nothing until the fetch resolves", async () => {
    let resolveFetch: (v: ChemistryStructure[]) => void = () => {};
    fetchStructuresMock.mockReturnValue(
      new Promise<ChemistryStructure[]>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    const { container } = render(<StructurePlate ingredient="albuterol sulfate" />);
    expect(container.querySelector(".plate")).toBeNull();

    await act(async () => {
      resolveFetch([structure()]);
    });
    await waitFor(() => expect(container.querySelector(".plate")).not.toBeNull());
  });

  it("renders the name, formula, MW, CID link href, and the provenance note", async () => {
    fetchStructuresMock.mockResolvedValue([structure()]);
    const { container, findByText } = render(<StructurePlate ingredient="albuterol sulfate" />);
    await findByText("albuterol sulfate");

    expect(container.querySelector(".plate__meta")?.textContent).toBe(
      "C26H44N2O10S · 576.7 g/mol",
    );
    const link = container.querySelector<HTMLAnchorElement>(".plate__src");
    expect(link?.getAttribute("href")).toBe("https://pubchem.ncbi.nlm.nih.gov/compound/39859");
    expect(link?.textContent).toContain("PubChem CID 39859");
    expect(container.querySelector(".plate__note")?.textContent).toBe(
      "Structure from PubChem; not part of the cited guidance.",
    );
    // The real drawing library (stubbed above) actually got invoked.
    expect(container.querySelector(".plate__art svg")).not.toBeNull();
  });

  it('appends "Parent compound shown." only when match is "parent"', async () => {
    fetchStructuresMock.mockResolvedValue([structure({ match: "parent" })]);
    const { findByText, container } = render(<StructurePlate ingredient="albuterol" />);
    await findByText("albuterol sulfate");
    expect(container.querySelector(".plate__note")?.textContent).toBe(
      "Structure from PubChem; not part of the cited guidance. Parent compound shown.",
    );
  });

  it("renders nothing on a rejected fetch", async () => {
    fetchStructuresMock.mockRejectedValue(new Error("network error"));
    const { container } = render(<StructurePlate ingredient="albuterol sulfate" />);
    await waitFor(() => expect(fetchStructuresMock).toHaveBeenCalled());
    // Let the rejection settle before asserting the negative.
    await act(async () => {});
    expect(container.querySelector(".plate")).toBeNull();
  });

  it("renders nothing on an empty list", async () => {
    fetchStructuresMock.mockResolvedValue([]);
    const { container } = render(<StructurePlate ingredient="albuterol sulfate" />);
    await waitFor(() => expect(fetchStructuresMock).toHaveBeenCalled());
    await act(async () => {});
    expect(container.querySelector(".plate")).toBeNull();
  });

  it("aborts the in-flight request on unmount", () => {
    fetchStructuresMock.mockReturnValue(new Promise(() => {})); // never resolves
    const { unmount } = render(<StructurePlate ingredient="albuterol sulfate" />);
    expect(fetchStructuresMock).toHaveBeenCalledTimes(1);
    const signal = fetchStructuresMock.mock.calls[0]?.[1] as AbortSignal;
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
  });

  it("re-fetches (and aborts the prior request) when ingredient changes", () => {
    fetchStructuresMock.mockReturnValue(new Promise(() => {}));
    const { rerender } = render(<StructurePlate ingredient="albuterol sulfate" />);
    const firstSignal = fetchStructuresMock.mock.calls[0]?.[1] as AbortSignal;
    rerender(<StructurePlate ingredient="fluticasone propionate" />);
    expect(firstSignal.aborted).toBe(true);
    expect(fetchStructuresMock).toHaveBeenCalledTimes(2);
    expect(fetchStructuresMock.mock.calls[1]?.[0]).toBe("fluticasone propionate");
  });

  it("renders one plate per structure up to 2, and omits the note in compact mode", async () => {
    fetchStructuresMock.mockResolvedValue([
      structure({ pubchem_cid: 1 }),
      structure({ pubchem_cid: 2 }),
      structure({ pubchem_cid: 3 }),
    ]);
    const { container } = render(<StructurePlate ingredient="albuterol sulfate" compact />);
    await waitFor(() => expect(container.querySelectorAll(".plate")).toHaveLength(2));
    expect(container.querySelector(".plate--compact")).not.toBeNull();
    expect(container.querySelector(".plate__note")).toBeNull();
  });
});
