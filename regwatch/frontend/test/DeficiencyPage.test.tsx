// Deficiency workflow-surface tests (surface 05). Locks the upload guard, the
// analyze -> poll -> report loop (pending -> running -> complete), the tiered
// rendering of the fault report, the failure paths (a failed run shows its own
// error, the poll deadline says the run is still running), and the runs list.
// Mock/fixture structure follows WhitepaperRunsPage.test.tsx: importOriginal is
// spread so ApiError stays the real class the page instanceof-checks.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  DeficiencyAnalyzeAccepted,
  DeficiencyRunDetail,
  DeficiencyRunList,
  DeficiencyRunSummary,
  Fault,
  FaultReport,
} from "@/lib/deficiency-types";

// The factories below only CALL these at runtime (after vi.mock hoists), so
// they never read them during hoist.
const analyzeDeficiencyMock = vi.fn<(file: File) => Promise<DeficiencyAnalyzeAccepted>>();
const listDeficiencyRunsMock = vi.fn<() => Promise<DeficiencyRunList>>();
const getDeficiencyRunMock = vi.fn<(id: number) => Promise<DeficiencyRunDetail>>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    analyzeDeficiency: (...a: Parameters<typeof analyzeDeficiencyMock>) => analyzeDeficiencyMock(...a),
    listDeficiencyRuns: (...a: Parameters<typeof listDeficiencyRunsMock>) => listDeficiencyRunsMock(...a),
    getDeficiencyRun: (...a: Parameters<typeof getDeficiencyRunMock>) => getDeficiencyRunMock(...a),
  };
});

import DeficiencyPage from "@/app/(shell)/deficiency/page";

function makeSummary(overrides: Partial<DeficiencyRunSummary> = {}): DeficiencyRunSummary {
  return {
    id: 7,
    filename: "module-3-2-P.pdf",
    status: "complete",
    created_at: "2026-07-30T10:00:00Z",
    completed_at: "2026-07-30T10:04:00Z",
    page_count: 42,
    fault_count: 3,
    error: null,
    ...overrides,
  };
}

function makeFault(overrides: Partial<Fault> = {}): Fault {
  return {
    title: "Assay release limit is wider than the monograph",
    detail: "The proposed release limit exceeds the compendial limit for the same test.",
    category: "spec_mismatch",
    severity: "high",
    tier: "verified",
    evidence_class: "code_verified",
    confidence: 0.91,
    evidence: "Assay: 90.0 - 110.0 percent of label claim",
    section: "3.2.P.5.1",
    page: 12,
    table_ref: "Table 16",
    source: "oracle:result_vs_limit",
    guidance_refs: ["ICH Q6A"],
    precedents: [],
    novel: false,
    out_of_distribution: false,
    challenge_note: "",
    ...overrides,
  };
}

// One fault per tier, so the group ordering and the per-tier headings are both
// exercised by a single report.
function makeReport(overrides: Partial<FaultReport> = {}): FaultReport {
  return {
    job_id: "job-abc",
    faults: [
      makeFault(),
      makeFault({
        title: "Unspecified impurity is not qualified",
        tier: "corroborated",
        evidence_class: "quote_anchored",
        severity: "medium",
        category: "impurity_qualification",
        evidence: "Any unspecified impurity: NMT 0.20 percent",
        section: "3.2.P.5.5",
        page: 18,
        table_ref: "",
        precedents: [
          {
            anda_number: "ANDA 090123",
            product_name: "Diclofenac sodium gel",
            deficiency_text: "Qualify the unspecified impurity at 0.15 percent.",
            similarity_score: 0.82,
          },
        ],
      }),
      // No evidence span and no precedent: the advisory card must still render
      // (recall lives in this tier, so an unanchored finding is not dropped).
      makeFault({
        title: "Container closure extractables are not addressed",
        tier: "advisory",
        evidence_class: "model_judgment",
        severity: "low",
        category: "container_extractables",
        evidence: "",
        section: "3.2.P.7",
        page: 22,
        table_ref: "",
        guidance_refs: [],
      }),
    ],
    faults_found: true,
    domains_checked: ["specifications", "impurities"],
    parse_failures: [],
    analysis_seconds: 42.5,
    ...overrides,
  };
}

function makeDetail(overrides: Partial<DeficiencyRunDetail> = {}): DeficiencyRunDetail {
  return { ...makeSummary(), report: null, ...overrides };
}

function pdf(name = "module-3-2-P.pdf"): File {
  return new File(["%PDF-1.4 fixture"], name, { type: "application/pdf" });
}

// The polling tests run on fake timers, and user-event's internal waits
// deadlock against a faked clock (its own advanceTimers hook never gets to run
// inside an awaited pointer sequence). fireEvent touches no timers at all, so
// the ONLY thing moving time in those tests is the explicit advance -- which is
// exactly the determinism they need. The non-polling tests keep user-event.
async function pick(file: File) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText("PDF file"), { target: { files: [file] } });
  });
}

async function submit() {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Analyze" }));
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("DeficiencyPage -- upload, poll, report (surface 05)", () => {
  it("renders the runs list: a complete run opens, a failed run shows its error", async () => {
    listDeficiencyRunsMock.mockResolvedValue({
      runs: [
        makeSummary(),
        makeSummary({
          id: 6,
          filename: "broken-scan.pdf",
          status: "failed",
          completed_at: "2026-07-29T09:02:00Z",
          page_count: null,
          fault_count: null,
          error: "pdftotext exited 1: no extractable text",
        }),
      ],
    });

    render(<DeficiencyPage />);

    // A complete run is the only openable row (it is the only one with a
    // report); the failed one renders as plain text.
    expect(await screen.findByRole("button", { name: "module-3-2-P.pdf" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "broken-scan.pdf" })).toBeNull();
    expect(screen.getByText("broken-scan.pdf")).toBeInTheDocument();

    expect(screen.getByText("complete")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("3 faults")).toBeInTheDocument();
    expect(screen.getByText("42 pages")).toBeInTheDocument();
    // The failure reason is on the row -- a failed run never reads as empty.
    expect(screen.getByText("pdftotext exited 1: no extractable text")).toBeInTheDocument();
  });

  it("upload -> analyze -> poll through pending/running/complete renders the tier groups", async () => {
    vi.useFakeTimers();
    listDeficiencyRunsMock.mockResolvedValue({ runs: [] });
    analyzeDeficiencyMock.mockResolvedValue({ run_id: 12, status: "pending" });
    getDeficiencyRunMock
      .mockResolvedValueOnce(makeDetail({ id: 12, status: "pending", completed_at: null, page_count: null, fault_count: null }))
      .mockResolvedValueOnce(makeDetail({ id: 12, status: "running", completed_at: null, page_count: null, fault_count: null }))
      .mockResolvedValueOnce(makeDetail({ id: 12, status: "complete", report: makeReport() }));

    render(<DeficiencyPage />);
    await act(async () => {}); // flush the mount runs-list load

    const file = pdf();
    await pick(file);
    await submit();

    expect(analyzeDeficiencyMock).toHaveBeenCalledWith(file);
    expect(screen.getByText("run #12")).toBeInTheDocument();

    // Tick 1: still pending. Nothing is polled before the first interval.
    expect(getDeficiencyRunMock).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(getDeficiencyRunMock).toHaveBeenCalledWith(12);
    expect(getDeficiencyRunMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Queued\./)).toBeInTheDocument();

    // Tick 2: running.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(screen.getByText(/Reading the document/)).toBeInTheDocument();

    // Tick 3: complete -> the report renders and polling stops.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(getDeficiencyRunMock).toHaveBeenCalledTimes(3);

    // Tier groups, in the contract order verified -> corroborated -> advisory.
    const groups = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(groups).toEqual(["Verified", "Corroborated", "Advisory"]);
    expect(screen.getByText("Assay release limit is wider than the monograph")).toBeInTheDocument();
    expect(screen.getByText("Unspecified impurity is not qualified")).toBeInTheDocument();
    expect(screen.getByText("Container closure extractables are not addressed")).toBeInTheDocument();
    // Evidence-class badge + the verbatim evidence span the finding rests on.
    expect(screen.getByText("code verified")).toBeInTheDocument();
    expect(screen.getByText("Assay: 90.0 - 110.0 percent of label claim")).toBeInTheDocument();
    expect(screen.getByText("3.2.P.5.1 - p.12 - Table 16")).toBeInTheDocument();

    // Polling ended -> the runs list was refreshed (mount + end).
    expect(listDeficiencyRunsMock).toHaveBeenCalledTimes(2);

    // And it really stopped: more time buys no more polls.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getDeficiencyRunMock).toHaveBeenCalledTimes(3);
  });

  it("a run that fails while polling shows the run's own error and refreshes the list", async () => {
    vi.useFakeTimers();
    listDeficiencyRunsMock.mockResolvedValue({ runs: [] });
    analyzeDeficiencyMock.mockResolvedValue({ run_id: 13, status: "pending" });
    getDeficiencyRunMock.mockResolvedValue(
      makeDetail({
        id: 13,
        status: "failed",
        fault_count: null,
        page_count: null,
        error: "the analyzer ran out of memory on page 210",
      }),
    );

    render(<DeficiencyPage />);
    await act(async () => {});
    await pick(pdf());
    await submit();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });

    expect(screen.getByText("the analyzer ran out of memory on page 210")).toBeInTheDocument();
    expect(screen.getByText(/The analysis failed\./)).toBeInTheDocument();
    // No report is rendered for a failed run -- a failure is never an empty
    // clean report.
    expect(screen.queryByText("Fault report")).toBeNull();
    expect(listDeficiencyRunsMock).toHaveBeenCalledTimes(2);
    // Terminal: polling stopped.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getDeficiencyRunMock).toHaveBeenCalledTimes(1);
  });

  it("stops polling after 600s and says the run is still running", async () => {
    vi.useFakeTimers();
    listDeficiencyRunsMock.mockResolvedValue({ runs: [] });
    analyzeDeficiencyMock.mockResolvedValue({ run_id: 14, status: "pending" });
    getDeficiencyRunMock.mockResolvedValue(
      makeDetail({ id: 14, status: "running", completed_at: null, page_count: null, fault_count: null }),
    );

    render(<DeficiencyPage />);
    await act(async () => {});
    await pick(pdf());
    await submit();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(600_000);
    });
    expect(screen.getByText("Still running - check the runs list later.")).toBeInTheDocument();
    const polls = getDeficiencyRunMock.mock.calls.length;

    // Abandoned, not cancelled: no further polls, and the upload form is usable
    // again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(getDeficiencyRunMock.mock.calls.length).toBe(polls);
    expect(screen.getByRole("button", { name: "Analyze" })).not.toBeDisabled();
  });

  it("refuses an oversize file client-side -- nothing is uploaded", async () => {
    const user = userEvent.setup();
    listDeficiencyRunsMock.mockResolvedValue({ runs: [] });

    render(<DeficiencyPage />);

    const big = pdf("huge.pdf");
    // A real 50MB+ fixture would be absurd in a unit test; the guard reads
    // File.size, so overriding it exercises exactly the same branch.
    Object.defineProperty(big, "size", { value: 60 * 1024 * 1024 });
    await user.upload(screen.getByLabelText("PDF file"), big);

    expect(await screen.findByText("That file is 60.0 MB; the limit is 50 MB.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Analyze" })).toBeDisabled();
    expect(analyzeDeficiencyMock).not.toHaveBeenCalled();
  });

  it("clicking a complete run loads and renders its report", async () => {
    const user = userEvent.setup();
    listDeficiencyRunsMock.mockResolvedValue({ runs: [makeSummary()] });
    getDeficiencyRunMock.mockResolvedValue(
      makeDetail({
        status: "complete",
        report: makeReport({
          parse_failures: [
            {
              layer: "specialist:elemental_impurities",
              reason: "strict decode failed after repair",
              raw_output: "{\"faults\": [",
              validation_error: "Expecting value: line 1 column 13",
              requires_human_review: true,
            },
          ],
        }),
      }),
    );

    render(<DeficiencyPage />);
    await user.click(await screen.findByRole("button", { name: "module-3-2-P.pdf" }));

    await waitFor(() => expect(getDeficiencyRunMock).toHaveBeenCalledWith(7));
    expect(await screen.findByText("Fault report")).toBeInTheDocument();
    expect(screen.getByText("Assay release limit is wider than the monograph")).toBeInTheDocument();
    expect(screen.getByText("3 candidate faults")).toBeInTheDocument();
    // Precedent citations ride with the corroborated fault.
    expect(screen.getByText("ANDA 090123")).toBeInTheDocument();
    expect(screen.getByText("- Diclofenac sodium gel")).toBeInTheDocument();
    expect(screen.getByText("82% match")).toBeInTheDocument();
    // A layer whose output could not be validated is a human-review card, never
    // a silent drop and never a raw model dump passing for a finding.
    expect(screen.getByRole("heading", { level: 3, name: "Needs human review" })).toBeInTheDocument();
    expect(
      screen.getByText("specialist:elemental_impurities: strict decode failed after repair"),
    ).toBeInTheDocument();
  });
});
