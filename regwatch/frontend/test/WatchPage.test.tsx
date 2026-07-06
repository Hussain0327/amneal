// First frontend test for the Watch page (surface 03, "the change bulletin").
// Locks the render / empty / error states and the #4/#9 card affordances
// (detected date, New vs Revised chip, confidence as a percent) so they cannot
// silently regress. Also locks the run-freshness line (last_run), the
// scope-from-alert chip, the New/Revised facet, and the "showing newest N of
// M" window honesty line.
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AlertRecord, ProductsResponse, WatchLatest, WatchRunSummary } from "@/lib/api";

// WatchPage calls these two named exports; the factory only CALLS the module
// consts at runtime (after vi.mock hoists), so it never reads them during hoist.
const watchLatest = vi.fn<() => Promise<WatchLatest>>();
const listProducts = vi.fn<() => Promise<ProductsResponse>>();
vi.mock("@/lib/api", () => ({
  watchLatest: () => watchLatest(),
  listProducts: () => listProducts(),
}));

// The page (alert scope chips) and its WatchlistTable consume useCurrentProduct,
// which throws without a provider; stub it so the page mounts without pulling in
// next/navigation. `scope` is mutable so tests can exercise the scoped-state
// reflection; the factory reads it at call time (after hoist), like the mocks
// above.
const setProduct = vi.fn();
let scope = { referenceProductName: "", applicationNumber: "" };
vi.mock("@/components/CurrentProductProvider", () => ({
  useCurrentProduct: () => ({
    referenceProductName: scope.referenceProductName,
    applicationNumber: scope.applicationNumber,
    hasProduct: Boolean(scope.referenceProductName || scope.applicationNumber),
    setProduct,
    clearProduct: vi.fn(),
    productParams: "",
  }),
}));

import WatchPage from "@/app/(shell)/watch/page";

function makeAlert(overrides: Partial<AlertRecord> = {}): AlertRecord {
  return {
    product_id: 1,
    active_ingredient: "Albuterol Sulfate",
    listing_appl_no: "NDA020503",
    listing_psg_type: "final",
    psg_document_id: 10,
    psg_version_id: 2,
    captured_at: "2026-06-01T12:00:00Z",
    // 19/21 -> the ugly float that used to leak verbatim onto the card.
    diff_summary: "Strength table updated; dissolution method revised.",
    confidence: 0.9047619047619048,
    rationale: "canonical",
    source_url: "https://www.fda.test/psg.pdf",
    ...overrides,
  };
}

// Fresh by default (finished a minute ago) so unrelated tests never trip the
// 48h staleness warning; relative timestamps keep the fixture from rotting.
function makeRun(overrides: Partial<WatchRunSummary> = {}): WatchRunSummary {
  const now = Date.now();
  return {
    started_at: new Date(now - 5 * 60_000).toISOString(),
    finished_at: new Date(now - 60_000).toISOString(),
    listings: 70,
    matched: 12,
    added: 1,
    revised: 2,
    unchanged: 9,
    errors: 0,
    alerts: 3,
    ...overrides,
  };
}

// Faithful to the real /watch/latest wire: a newest-first page (count = page
// size, total = full ledger, limit/offset = the window) plus last_run telemetry.
function makeFeed(alerts: AlertRecord[], overrides: Partial<WatchLatest> = {}): WatchLatest {
  return {
    count: alerts.length,
    total: alerts.length,
    limit: 200,
    offset: 0,
    alerts,
    last_run: makeRun(),
    ...overrides,
  };
}

const NO_PRODUCTS: ProductsResponse = { count: 0, products: [] };

afterEach(() => {
  vi.clearAllMocks();
  scope = { referenceProductName: "", applicationNumber: "" };
});

describe("WatchPage -- the change bulletin (surface 03)", () => {
  it("renders an alert with date, Revised chip, and confidence as a percent", async () => {
    watchLatest.mockResolvedValue(makeFeed([makeAlert()]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    const { container } = render(<WatchPage />);

    expect(await screen.findByText("Albuterol Sulfate")).toBeInTheDocument();
    expect(screen.getByText(/NDA020503/)).toBeInTheDocument();
    // #9: confidence rounded to a whole percent, not the raw 0.9047619... float.
    expect(screen.getByText("90% match")).toBeInTheDocument();
    expect(screen.queryByText(/0\.904/)).toBeNull();
    // #9: a revision summary (not the initial marker) -> "Revised" chip. The
    // selector pins the CARD chip (a span); the facet buttons above the list
    // carry the same labels.
    expect(screen.getByText("Revised", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByText("New", { selector: "span" })).toBeNull();
    // #4: a machine-readable <time> carrying captured_at, rendered date-only
    // (UTC). Scoped to the card -- the freshness line renders its own <time>.
    const time = container.querySelector("article time");
    expect(time).not.toBeNull();
    expect(time).toHaveAttribute("dateTime", "2026-06-01T12:00:00Z");
    expect(time?.textContent).toContain("2026");
    // The empty state must NOT show when alerts exist.
    expect(screen.queryByText(/No alerts yet/)).toBeNull();
  });

  it("labels a first-version alert as New", async () => {
    watchLatest.mockResolvedValue(
      makeFeed([makeAlert({ diff_summary: "Initial version ingested. Begins: ..." })]),
    );
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText("New", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByText("Revised", { selector: "span" })).toBeNull();
  });

  it("shows the empty state when the feed is loaded-but-empty", async () => {
    watchLatest.mockResolvedValue(makeFeed([]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText(/No alerts yet/)).toBeInTheDocument();
    expect(screen.queryByText("Feed unavailable")).toBeNull();
  });

  it("shows the Feed unavailable stamp on a rejected load, not the empty state", async () => {
    watchLatest.mockRejectedValue(new Error("backend down"));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText("Feed unavailable")).toBeInTheDocument();
    expect(screen.getByText("backend down")).toBeInTheDocument();
    // An error must never masquerade as a loaded-but-empty feed.
    expect(screen.queryByText(/No alerts yet/)).toBeNull();
  });

  it("re-fetches the feed when Refresh is clicked", async () => {
    watchLatest.mockResolvedValue(makeFeed([]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);
    await screen.findByText(/No alerts yet/);
    // Refresh is disabled while a load is in flight; wait for the initial load
    // to settle before clicking.
    const refresh = screen.getByRole("button", { name: /refresh/i });
    await waitFor(() => expect(refresh).toBeEnabled());

    await userEvent.click(refresh);

    await waitFor(() => expect(watchLatest).toHaveBeenCalledTimes(2));
  });

  it("re-fetches on tab focus, and the in-flight guard dedupes the focus+visibilitychange pair", async () => {
    watchLatest.mockResolvedValue(makeFeed([]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);
    await screen.findByText(/No alerts yet/);
    await waitFor(() => expect(screen.getByRole("button", { name: /refresh/i })).toBeEnabled());
    expect(watchLatest).toHaveBeenCalledTimes(1); // initial mount load

    // A real tab-return fires BOTH events synchronously; the guard must collapse
    // them into a SINGLE refetch, not two. Wrap in act() because the dispatch
    // synchronously flips the busy state.
    act(() => {
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await waitFor(() => expect(watchLatest).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("button", { name: /refresh/i })).toBeEnabled());
    expect(watchLatest).toHaveBeenCalledTimes(2); // no third call from the paired event
  });

  it("prefers change_kind over a degraded initial-marker summary (prod revision renders Revised)", async () => {
    // Prod revisions lose the prior parsed text (ephemeral cron runner disk),
    // so diff_summary degrades to the first-version marker; the structural
    // change_kind from version history must win over the prose heuristic.
    watchLatest.mockResolvedValue(
      makeFeed([
        makeAlert({
          change_kind: "revised",
          diff_summary: "Initial version ingested. Begins: ...",
        }),
      ]),
    );
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText("Revised", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByText("New", { selector: "span" })).toBeNull();
  });

  it("renders New from change_kind regardless of summary prose", async () => {
    watchLatest.mockResolvedValue(
      makeFeed([makeAlert({ change_kind: "new", diff_summary: "Strength table updated." })]),
    );
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText("New", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByText("Revised", { selector: "span" })).toBeNull();
  });

  it("disables the feed Try again while the sibling products request is still in flight", async () => {
    // The stamp renders as soon as the alerts fetch rejects, but busy stays
    // true until BOTH requests settle; during that window load() no-ops behind
    // the loadingRef guard, so the button must be disabled, not silently dead.
    watchLatest.mockRejectedValue(new Error("backend down"));
    let resolveProducts!: (v: ProductsResponse) => void;
    listProducts.mockReturnValue(
      new Promise<ProductsResponse>((resolve) => {
        resolveProducts = resolve;
      }),
    );

    render(<WatchPage />);

    const tryAgain = await screen.findByRole("button", { name: /try again/i });
    expect(tryAgain).toBeDisabled();

    await act(async () => {
      resolveProducts(NO_PRODUCTS);
    });
    await waitFor(() => expect(tryAgain).toBeEnabled());

    // Once enabled the retry is real: a click reaches the API again.
    await userEvent.click(tryAgain);
    await waitFor(() => expect(watchLatest).toHaveBeenCalledTimes(2));
  });

  it("disables the watchlist Try again while the sibling feed request is still in flight", async () => {
    let resolveAlerts!: (v: WatchLatest) => void;
    watchLatest.mockReturnValue(
      new Promise<WatchLatest>((resolve) => {
        resolveAlerts = resolve;
      }),
    );
    listProducts.mockRejectedValue(new Error("products down"));

    render(<WatchPage />);

    // Only the watchlist error (with its Try again) is on screen: the feed is
    // still pending, so the stamp's button cannot shadow this query.
    const tryAgain = await screen.findByRole("button", { name: /try again/i });
    expect(tryAgain).toBeDisabled();

    await act(async () => {
      resolveAlerts(makeFeed([]));
    });
    await waitFor(() => expect(tryAgain).toBeEnabled());
  });

  it("empty-watchlist copy describes real population (import / products API), not a scoping side effect", async () => {
    watchLatest.mockResolvedValue(makeFeed([]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText(/watchlist is empty/i)).toBeInTheDocument();
    // Scoping only sets rp/appl page context and never writes a watchlist row;
    // the copy must not promise that scoping "starts tracking" a product.
    expect(screen.queryByText(/start tracking/i)).toBeNull();
    expect(screen.getByText(/Drugs@FDA import/i)).toBeInTheDocument();
  });

  it("labels a null-summary alert as Revised (the wire shape for a summary-less row)", async () => {
    watchLatest.mockResolvedValue(makeFeed([makeAlert({ diff_summary: null })]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText("Revised", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByText("New", { selector: "span" })).toBeNull();
  });

  it("renders nothing (not a wrong value) for a 0 confidence and an unparseable timestamp", async () => {
    watchLatest.mockResolvedValue(makeFeed([makeAlert({ confidence: 0, captured_at: "not-a-date" })]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    const { container } = render(<WatchPage />);
    await screen.findByText("Albuterol Sulfate");

    expect(screen.queryByText(/% match/)).toBeNull(); // not "0% match"
    // The card's detected-date line is absent: assert on the CARD's <time>
    // (the freshness line above the list legitimately renders its own).
    expect(container.querySelector("article time")).toBeNull(); // not "Invalid Date"
  });

  // ---- run freshness (last_run) ----

  it("shows Last checked from a fresh last_run, independent of the empty alert list", async () => {
    const run = makeRun();
    watchLatest.mockResolvedValue(makeFeed([], { last_run: run }));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    const { container } = render(<WatchPage />);

    expect(await screen.findByText(/Last checked/)).toBeInTheDocument();
    // Machine-readable finished_at on the freshness <time>.
    expect(container.querySelector("time")).toHaveAttribute("dateTime", run.finished_at);
    // Fresh + clean run: no staleness or error caveats, and no "never ran" note.
    expect(screen.queryByText(/out of date/)).toBeNull();
    expect(screen.queryByText(/ingest error/)).toBeNull();
    expect(screen.queryByText(/No watch run recorded/)).toBeNull();
    // Run recency is not the list's emptiness: both lines coexist.
    expect(screen.getByText(/No alerts yet/)).toBeInTheDocument();
    // total == page size -> the window line must not pretend truncation.
    expect(screen.queryByText(/Showing newest/)).toBeNull();
  });

  it("says no watch run has been recorded when last_run is null", async () => {
    watchLatest.mockResolvedValue(makeFeed([], { last_run: null }));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText(/No watch run recorded yet/)).toBeInTheDocument();
    // INV-4: never imply a check happened.
    expect(screen.queryByText(/Last checked/)).toBeNull();
  });

  it("warns that the feed may be out of date when the last run finished over 48h ago", async () => {
    watchLatest.mockResolvedValue(
      makeFeed([], {
        last_run: makeRun({
          started_at: new Date(Date.now() - 73 * 3_600_000).toISOString(),
          finished_at: new Date(Date.now() - 72 * 3_600_000).toISOString(),
        }),
      }),
    );
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText(/Last checked/)).toBeInTheDocument();
    expect(screen.getByText(/the feed may be out of date/)).toBeInTheDocument();
  });

  it("notes ingest errors from the last run (the bulletin may be incomplete)", async () => {
    watchLatest.mockResolvedValue(makeFeed([], { last_run: makeRun({ errors: 3 }) }));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText(/hit 3 ingest errors/)).toBeInTheDocument();
    expect(screen.getByText(/the bulletin may be incomplete/)).toBeInTheDocument();
    // A fresh-but-errored run is an errors story, not a staleness one.
    expect(screen.queryByText(/out of date/)).toBeNull();
  });

  // ---- scope-from-alert ----

  it("scope on an alert card writes the canonical (name, six-digit appl) pair", async () => {
    watchLatest.mockResolvedValue(makeFeed([makeAlert()]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    const scopeBtn = await screen.findByRole("button", { name: "scope" });
    await userEvent.click(scopeBtn);

    // NDA020503 canonicalizes to 020503 -- the SAME pair the watchlist table
    // and the top bar write, so scoping from an alert matches everywhere.
    expect(setProduct).toHaveBeenCalledWith({
      referenceProductName: "Albuterol Sulfate",
      applicationNumber: "020503",
    });
  });

  it("reflects the active scope on the alert card as a pressed 'scoped' chip", async () => {
    scope = { referenceProductName: "Albuterol Sulfate", applicationNumber: "020503" };
    watchLatest.mockResolvedValue(makeFeed([makeAlert()]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    const scoped = await screen.findByRole("button", { name: "scoped" });
    expect(scoped).toHaveAttribute("aria-pressed", "true");
  });

  // ---- New/Revised facet ----

  it("filters the bulletin by kind and keeps the entries counter honest", async () => {
    watchLatest.mockResolvedValue(
      makeFeed([
        makeAlert({ psg_document_id: 10, change_kind: "new", active_ingredient: "Albuterol Sulfate" }),
        makeAlert({ psg_document_id: 11, change_kind: "revised", active_ingredient: "Budesonide" }),
        makeAlert({ psg_document_id: 12, change_kind: "revised", active_ingredient: "Cetirizine" }),
      ]),
    );
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);
    await screen.findByText("Albuterol Sulfate");
    expect(screen.getByText("3 entries")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "New" }));
    expect(screen.getByText("1 of 3 entries")).toBeInTheDocument();
    expect(screen.getByText("Albuterol Sulfate")).toBeInTheDocument();
    expect(screen.queryByText("Budesonide")).toBeNull();
    expect(screen.queryByText("Cetirizine")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Revised" }));
    expect(screen.getByText("2 of 3 entries")).toBeInTheDocument();
    expect(screen.queryByText("Albuterol Sulfate")).toBeNull();
    expect(screen.getByText("Budesonide")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "All" }));
    expect(screen.getByText("3 entries")).toBeInTheDocument();
    expect(screen.getByText("Albuterol Sulfate")).toBeInTheDocument();
  });

  it("a zero-match facet says so instead of reading as an empty feed", async () => {
    watchLatest.mockResolvedValue(makeFeed([makeAlert({ change_kind: "revised" })]));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);
    await screen.findByText("Albuterol Sulfate");

    await userEvent.click(screen.getByRole("button", { name: "New" }));

    expect(screen.getByText("0 of 1 entries")).toBeInTheDocument();
    expect(screen.getByText(/No new entries in the current feed/)).toBeInTheDocument();
    // The real empty state stays reserved for a genuinely empty feed.
    expect(screen.queryByText(/No alerts yet/)).toBeNull();
  });

  // ---- window honesty (total/limit/offset) ----

  it("admits the page is a window when total exceeds the alerts on screen", async () => {
    watchLatest.mockResolvedValue(makeFeed([makeAlert()], { total: 12 }));
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText(/Showing newest 1 of 12 entries/)).toBeInTheDocument();
  });
});
