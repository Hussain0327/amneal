// White Paper workflow-surface tests (surface 04, durable runs). Locks the
// Phase-2 contract: the org-shared runs list, ?run= as the state of record,
// the attributed analyst overlay (edit -> save -> attribution), the
// finalize-freeze, the run-backed .docx download, and the degraded
// (run_id null) populate that renders inline instead of navigating.
// Mock/fixture structure follows WatchPage.test.tsx.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  WhitepaperCell,
  WhitepaperCellSaved,
  WhitepaperResponse,
  WhitepaperRunDetail,
  WhitepaperRunList,
  WhitepaperRunSummary,
  WhitepaperSpine,
} from "@/lib/api";

// The factories below only CALL these at runtime (after vi.mock hoists), so
// they never read them during hoist -- same pattern as WatchPage.test.tsx.
const listWhitepaperRunsMock = vi.fn<() => Promise<WhitepaperRunList>>();
const getWhitepaperRunMock = vi.fn<(id: number) => Promise<WhitepaperRunDetail>>();
const buildWhitepaperMock = vi.fn<(rld: string, appl: string) => Promise<WhitepaperResponse>>();
const saveWhitepaperInputMock =
  vi.fn<(runId: number, cellId: string, value: string) => Promise<WhitepaperCellSaved>>();
const clearWhitepaperInputMock =
  vi.fn<(runId: number, cellId: string) => Promise<WhitepaperCellSaved>>();
const finalizeWhitepaperRunMock = vi.fn<(id: number) => Promise<unknown>>();
const reopenWhitepaperRunMock = vi.fn<(id: number) => Promise<unknown>>();
const deleteWhitepaperRunMock = vi.fn<(id: number) => Promise<void>>();
const downloadWhitepaperDocxMock = vi.fn<(id: number, appl: string) => Promise<void>>();

// Keep everything else real -- the page uses ApiError from this module and the
// tests must throw the same class the page instanceof-checks.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listWhitepaperRuns: (...a: Parameters<typeof listWhitepaperRunsMock>) =>
      listWhitepaperRunsMock(...a),
    getWhitepaperRun: (...a: Parameters<typeof getWhitepaperRunMock>) =>
      getWhitepaperRunMock(...a),
    buildWhitepaper: (...a: Parameters<typeof buildWhitepaperMock>) => buildWhitepaperMock(...a),
    saveWhitepaperInput: (...a: Parameters<typeof saveWhitepaperInputMock>) =>
      saveWhitepaperInputMock(...a),
    clearWhitepaperInput: (...a: Parameters<typeof clearWhitepaperInputMock>) =>
      clearWhitepaperInputMock(...a),
    finalizeWhitepaperRun: (...a: Parameters<typeof finalizeWhitepaperRunMock>) =>
      finalizeWhitepaperRunMock(...a),
    reopenWhitepaperRun: (...a: Parameters<typeof reopenWhitepaperRunMock>) =>
      reopenWhitepaperRunMock(...a),
    deleteWhitepaperRun: (...a: Parameters<typeof deleteWhitepaperRunMock>) =>
      deleteWhitepaperRunMock(...a),
    downloadWhitepaperDocx: (...a: Parameters<typeof downloadWhitepaperDocxMock>) =>
      downloadWhitepaperDocxMock(...a),
  };
});

// URL `run` param under test control; rerendering re-reads it, which is how a
// row click / browser back-forward reaches the URL-sync effect.
let urlRunParam: string | null = null;
const routerReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace }),
  usePathname: () => "/whitepaper",
  useSearchParams: () => new URLSearchParams(urlRunParam ? { run: urlRunParam } : {}),
}));

// The page consumes useCurrentProduct (intake prefill), which throws without a
// provider; stub it so the page mounts without pulling in the real provider.
vi.mock("@/components/CurrentProductProvider", () => ({
  useCurrentProduct: () => ({
    referenceProductName: "",
    applicationNumber: "",
    hasProduct: false,
    setProduct: vi.fn(),
    clearProduct: vi.fn(),
    productParams: "",
  }),
}));

import WhitepaperPage from "@/app/(shell)/whitepaper/page";
import { ApiError } from "@/lib/api";

function makeSpine(overrides: Partial<WhitepaperSpine> = {}): WhitepaperSpine {
  return {
    application_number: "020503",
    application_type: "NDA",
    ingredient: "ALBUTEROL SULFATE",
    normalized_name: "albuterol sulfate hfa",
    product_numbers: ["001"],
    setid: null,
    spl_candidates: [],
    warnings: [],
    ...overrides,
  };
}

function makeCell(overrides: Partial<WhitepaperCell> = {}): WhitepaperCell {
  return {
    id: "dosage_form",
    label: "Dosage form",
    mode: "auto",
    status: "populated",
    value: "Inhalation aerosol",
    evidence: [],
    note: null,
    ...overrides,
  };
}

// One populated cell + one analyst cell: enough to exercise both overlay
// treatments (inline editor vs add-note annotation).
function makeSections() {
  return [
    {
      title: "Product overview",
      cells: [
        makeCell(),
        makeCell({
          id: "storage_conditions",
          label: "Storage conditions",
          mode: "manual",
          status: "analyst_input_required",
          value: null,
          note: "Analyst judgment required.",
        }),
      ],
    },
  ];
}

function makeDetail(overrides: Partial<WhitepaperRunDetail> = {}): WhitepaperRunDetail {
  return {
    id: 7,
    rld_name_input: "albuterol sulfate",
    application_number: "020503",
    application_type: "NDA",
    ingredient: "ALBUTEROL SULFATE",
    normalized_name: "albuterol sulfate hfa",
    spine: makeSpine(),
    sections: makeSections(),
    warnings: [],
    status: "draft",
    populated_count: 1,
    analyst_input_count: 1,
    verified_absent_count: 0,
    source_audit_id: 41,
    created_by: "Hana Analyst",
    created_by_user_id: 3,
    created_at: "2026-07-01T10:00:00Z",
    updated_at: "2026-07-01T10:00:00Z",
    finalized_at: null,
    finalized_by: null,
    inputs: {},
    ...overrides,
  };
}

function makeSummary(overrides: Partial<WhitepaperRunSummary> = {}): WhitepaperRunSummary {
  return {
    id: 7,
    rld_name_input: "albuterol sulfate",
    application_number: "020503",
    application_type: "NDA",
    ingredient: "ALBUTEROL SULFATE",
    normalized_name: "albuterol sulfate hfa",
    status: "draft",
    populated_count: 1,
    analyst_input_count: 1,
    verified_absent_count: 0,
    inputs_count: 0,
    created_by: "Hana Analyst",
    created_at: "2026-07-01T10:00:00Z",
    updated_at: "2026-07-01T10:00:00Z",
    ...overrides,
  };
}

function makeList(runs: WhitepaperRunSummary[], overrides: Partial<WhitepaperRunList> = {}): WhitepaperRunList {
  return { count: runs.length, total: runs.length, limit: 50, offset: 0, runs, ...overrides };
}

function makeBuilt(overrides: Partial<WhitepaperResponse> = {}): WhitepaperResponse {
  return {
    spine: makeSpine(),
    sections: makeSections(),
    warnings: [],
    audit_id: 41,
    run_id: 7,
    ...overrides,
  };
}

beforeEach(() => {
  // Every render mounts the runs list; default it to loaded-but-empty so
  // individual tests only override what they exercise.
  listWhitepaperRunsMock.mockResolvedValue(makeList([]));
});

afterEach(() => {
  vi.clearAllMocks();
  urlRunParam = null;
});

describe("WhitepaperPage -- durable runs workflow (surface 04)", () => {
  it("renders the org-shared runs list and a row click writes ?run= (scope-preserving replace)", async () => {
    listWhitepaperRunsMock.mockResolvedValue(makeList([makeSummary()]));

    render(<WhitepaperPage />);

    const open = await screen.findByRole("button", { name: "ALBUTEROL SULFATE" });
    // Row facts: status chip, counts, inputs, author.
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByText("1 populated / 0 absent / 1 analyst")).toBeInTheDocument();
    expect(screen.getByText("0 inputs")).toBeInTheDocument();
    expect(screen.getByText("by Hana Analyst")).toBeInTheDocument();

    await userEvent.click(open);

    // The URL is the state of record: opening a run is a router.replace that
    // only touches the run param (rp/appl and friends survive).
    expect(routerReplace).toHaveBeenCalledWith("/whitepaper?run=7", { scroll: false });
  });

  it("hydrates the run view from ?run=: verbatim cells, freshness line, inline analyst editor", async () => {
    urlRunParam = "7";
    getWhitepaperRunMock.mockResolvedValue(makeDetail());

    render(<WhitepaperPage />);

    expect(await screen.findByText(/run #7 \/ audit #41/)).toBeInTheDocument();
    expect(getWhitepaperRunMock).toHaveBeenCalledWith(7);
    // The generated layer renders exactly as populated.
    expect(screen.getByText("Inhalation aerosol")).toBeInTheDocument();
    // INV-5 honesty: an old draft admits its data age.
    expect(screen.getByText(/Data as of .* - re-populate to refresh/)).toBeInTheDocument();
    // The analyst cell carries an always-open inline editor...
    expect(screen.getByLabelText("Analyst input for storage_conditions")).toBeInTheDocument();
    // ...while the populated cell's note editor hides behind an affordance.
    expect(screen.getByRole("button", { name: "Add note" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finalize" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reopen" })).toBeNull();
  });

  it("saving an analyst cell calls saveWhitepaperInput and shows the attribution", async () => {
    urlRunParam = "7";
    getWhitepaperRunMock.mockResolvedValue(makeDetail());
    saveWhitepaperInputMock.mockResolvedValue({
      run_id: 7,
      cell_id: "storage_conditions",
      cleared: false,
      input: {
        value: "Store below 25C",
        author: "Hana Analyst",
        updated_at: new Date().toISOString(),
      },
    });

    render(<WhitepaperPage />);

    const editor = await screen.findByLabelText("Analyst input for storage_conditions");
    await userEvent.type(editor, "Store below 25C");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(saveWhitepaperInputMock).toHaveBeenCalledWith(7, "storage_conditions", "Store below 25C");
    // The stored view (author + relative time) renders after the save -- the
    // human answer is visibly human (INV-3).
    expect(await screen.findByText(/Saved by Hana Analyst/)).toBeInTheDocument();
  });

  it("a finalized run freezes every editor, renders the overlay read-only, and offers Reopen", async () => {
    urlRunParam = "7";
    getWhitepaperRunMock.mockResolvedValue(
      makeDetail({
        status: "final",
        finalized_at: "2026-07-02T09:00:00Z",
        finalized_by: "Hana Analyst",
        inputs: {
          storage_conditions: {
            value: "Store below 25C",
            author: "Hana Analyst",
            updated_at: "2026-07-01T12:00:00Z",
          },
        },
      }),
    );

    const { container } = render(<WhitepaperPage />);

    expect(await screen.findByRole("button", { name: "Reopen" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Finalize" })).toBeNull();
    // Frozen: no textarea, no add-note, no save -- the analyst layer is a record.
    expect(screen.queryByLabelText("Analyst input for storage_conditions")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add note" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    // The saved value still shows, attributed and visually distinct (the
    // .wp-analyst annotation block, never restyled into the cited value).
    expect(screen.getByText("Store below 25C")).toBeInTheDocument();
    const meta = container.querySelector(".wp-analyst__meta");
    expect(meta?.textContent).toContain("by Hana Analyst");
    expect(screen.getByText(/Finalized/)).toBeInTheDocument();
  });

  it("Download .docx renders FROM the saved run (run id + stored application number)", async () => {
    urlRunParam = "7";
    getWhitepaperRunMock.mockResolvedValue(makeDetail());
    downloadWhitepaperDocxMock.mockResolvedValue(undefined);

    render(<WhitepaperPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Download .docx" }));

    await waitFor(() => expect(downloadWhitepaperDocxMock).toHaveBeenCalledWith(7, "020503"));
  });

  it("a persisted populate navigates to the created run, pinning the canonical scope in the same write", async () => {
    buildWhitepaperMock.mockResolvedValue(makeBuilt({ run_id: 7 }));

    render(<WhitepaperPage />);
    await userEvent.type(screen.getByPlaceholderText("albuterol sulfate"), "albuterol");
    await userEvent.type(screen.getByPlaceholderText(/NDA 020503/), "020503");
    await userEvent.click(screen.getByRole("button", { name: "Populate white paper" }));

    // One replace writes rp/appl (the spine's CANONICAL identity) AND run
    // together -- two writes would race and wipe each other.
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith(
        "/whitepaper?rp=albuterol+sulfate+hfa&appl=020503&run=7",
        { scroll: false },
      ),
    );
    // The ephemeral inline view is NOT rendered for a persisted run.
    expect(screen.queryByText("not saved")).toBeNull();
  });

  it("a degraded populate (run_id null) still renders inline, with the persist warning and no editors", async () => {
    const warning = "Saving this run failed - the populate result below was not persisted.";
    buildWhitepaperMock.mockResolvedValue(makeBuilt({ run_id: null, warnings: [warning] }));

    render(<WhitepaperPage />);
    await userEvent.type(screen.getByPlaceholderText("albuterol sulfate"), "albuterol");
    await userEvent.type(screen.getByPlaceholderText(/NDA 020503/), "020503");
    await userEvent.click(screen.getByRole("button", { name: "Populate white paper" }));

    // The result is complete and shown -- durability degraded, populate did not.
    expect(await screen.findByText("not saved")).toBeInTheDocument();
    expect(screen.getByText(warning)).toBeInTheDocument();
    expect(screen.getByText("Inhalation aerosol")).toBeInTheDocument();
    // No overlay layer exists for an unpersisted result: nothing to attach
    // inputs to, nothing to download server-side.
    expect(screen.queryByRole("button", { name: "Add note" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Download .docx" })).toBeNull();
    // The URL write pins the scope but carries NO run param.
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith("/whitepaper?rp=albuterol+sulfate+hfa&appl=020503", {
        scroll: false,
      }),
    );
  });

  it("the blanks navigator counts the unfilled analyst cells and puts the caret in the next one", async () => {
    urlRunParam = "7";
    getWhitepaperRunMock.mockResolvedValue(makeDetail());

    render(<WhitepaperPage />);

    // One analyst cell, nothing saved against it yet.
    const next = await screen.findByRole("button", { name: "Next blank (1)" });
    await userEvent.click(next);

    expect(screen.getByLabelText("Analyst input for storage_conditions")).toHaveFocus();
  });

  it("a filled blank leaves the navigator nothing to walk to", async () => {
    urlRunParam = "7";
    getWhitepaperRunMock.mockResolvedValue(
      makeDetail({
        inputs: {
          storage_conditions: {
            value: "Store below 25C",
            author: "Hana Analyst",
            updated_at: "2026-07-01T12:00:00Z",
          },
        },
      }),
    );

    render(<WhitepaperPage />);

    await screen.findByText(/run #7 \/ audit #41/);
    expect(screen.queryByRole("button", { name: /Next blank/ })).toBeNull();
  });

  it("cited values carry a footnote into a provenance appendix that names every citing cell", async () => {
    urlRunParam = "7";
    const evidence = {
      source: "Orange Book",
      locator: "products.txt appl_no=020503",
      source_url: "https://www.fda.gov/orange-book",
      fetched_at: "2026-07-01T09:00:00Z",
      page: null,
      section: null,
      snippet: "ALBUTEROL SULFATE; AEROSOL, METERED",
    };
    getWhitepaperRunMock.mockResolvedValue(
      makeDetail({
        sections: [
          {
            title: "Product overview",
            cells: [makeCell({ evidence: [evidence] }), makeCell({ id: "route", label: "Route", value: "INHALATION", evidence: [evidence] })],
          },
        ],
      }),
    );

    render(<WhitepaperPage />);

    // The marker sits with the value and links to the appendix entry.
    const markers = await screen.findAllByRole("link", { name: "[1]" });
    expect(markers).toHaveLength(2);
    expect(markers[0]).toHaveAttribute("href", "#wp-ref-1");
    // One appendix entry for the one record, listing both cells that cite it.
    expect(screen.getByText("Cited at: Dosage form; Route")).toBeInTheDocument();
    expect(screen.getByText("ALBUTEROL SULFATE; AEROSOL, METERED")).toBeInTheDocument();
  });

  it("New paper closes the open run without touching the rest of the scope", async () => {
    urlRunParam = "7";
    getWhitepaperRunMock.mockResolvedValue(makeDetail());

    render(<WhitepaperPage />);

    await userEvent.click(await screen.findByRole("button", { name: "New paper" }));

    expect(routerReplace).toHaveBeenCalledWith("/whitepaper", { scroll: false });
  });

  it("the fill-in cascade plays on the document that was just populated", async () => {
    buildWhitepaperMock.mockResolvedValue(makeBuilt({ run_id: null }));

    const { container } = render(<WhitepaperPage />);
    await userEvent.type(screen.getByPlaceholderText("albuterol sulfate"), "albuterol");
    await userEvent.type(screen.getByPlaceholderText(/NDA 020503/), "020503");
    await userEvent.click(screen.getByRole("button", { name: "Populate white paper" }));

    await screen.findByText("not saved");
    expect(container.querySelectorAll(".wp-ink").length).toBeGreaterThan(0);
  });

  it("and never on a different run opened while that cascade is still pending", async () => {
    buildWhitepaperMock.mockResolvedValue(makeBuilt({ run_id: 7 }));
    getWhitepaperRunMock.mockResolvedValue(makeDetail({ id: 9 }));

    const { container, rerender } = render(<WhitepaperPage />);
    await userEvent.type(screen.getByPlaceholderText("albuterol sulfate"), "albuterol");
    await userEvent.type(screen.getByPlaceholderText(/NDA 020503/), "020503");
    await userEvent.click(screen.getByRole("button", { name: "Populate white paper" }));
    await waitFor(() => expect(routerReplace).toHaveBeenCalled());

    // Within the cascade's window the analyst opens a DIFFERENT saved run.
    urlRunParam = "9";
    rerender(<WhitepaperPage />);
    await screen.findByText(/run #9 \/ audit #41/);

    // Run 9 is not the document that was populated, so nothing inks in: the
    // animation is a fact about one document, not a mode the page is in.
    expect(container.querySelectorAll(".wp-ink")).toHaveLength(0);
  });

  it("delete refusals (403 foreign draft) surface inline -- the affordance never hides", async () => {
    listWhitepaperRunsMock.mockResolvedValue(makeList([makeSummary()]));
    deleteWhitepaperRunMock.mockRejectedValue(new ApiError(403, "only the run's creator may delete it"));

    render(<WhitepaperPage />);

    await userEvent.click(await screen.findByRole("button", { name: "delete" }));
    // Inline confirm, not a browser dialog.
    expect(screen.getByText("Delete this run?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "confirm" }));

    expect(deleteWhitepaperRunMock).toHaveBeenCalledWith(7);
    expect(await screen.findByText(/only the run's creator may delete it/)).toBeInTheDocument();
    // The row is still there -- nothing was deleted.
    expect(screen.getByRole("button", { name: "ALBUTEROL SULFATE" })).toBeInTheDocument();
  });
});
