// First frontend test for the Watch page (surface 03, "the change bulletin").
// Locks the render / empty / error states and the #4/#9 card affordances
// (detected date, New vs Revised chip, confidence as a percent) so they cannot
// silently regress.
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AlertRecord, ProductsResponse, WatchLatest } from "@/lib/api";

// WatchPage calls these two named exports; the factory only CALLS the module
// consts at runtime (after vi.mock hoists), so it never reads them during hoist.
const watchLatest = vi.fn<() => Promise<WatchLatest>>();
const listProducts = vi.fn<() => Promise<ProductsResponse>>();
vi.mock("@/lib/api", () => ({
  watchLatest: () => watchLatest(),
  listProducts: () => listProducts(),
}));

// The page's WatchlistTable consumes useCurrentProduct, which throws without a
// provider; stub it so the page mounts without pulling in next/navigation.
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

const NO_PRODUCTS: ProductsResponse = { count: 0, products: [] };

afterEach(() => {
  vi.clearAllMocks();
});

describe("WatchPage -- the change bulletin (surface 03)", () => {
  it("renders an alert with date, Revised chip, and confidence as a percent", async () => {
    watchLatest.mockResolvedValue({ count: 1, alerts: [makeAlert()] });
    listProducts.mockResolvedValue(NO_PRODUCTS);

    const { container } = render(<WatchPage />);

    expect(await screen.findByText("Albuterol Sulfate")).toBeInTheDocument();
    expect(screen.getByText(/NDA020503/)).toBeInTheDocument();
    // #9: confidence rounded to a whole percent, not the raw 0.9047619... float.
    expect(screen.getByText("90% match")).toBeInTheDocument();
    expect(screen.queryByText(/0\.904/)).toBeNull();
    // #9: a revision summary (not the initial marker) -> "Revised" chip.
    expect(screen.getByText("Revised")).toBeInTheDocument();
    expect(screen.queryByText("New")).toBeNull();
    // #4: a machine-readable <time> carrying captured_at, rendered date-only (UTC).
    const time = container.querySelector("time");
    expect(time).not.toBeNull();
    expect(time).toHaveAttribute("dateTime", "2026-06-01T12:00:00Z");
    expect(time?.textContent).toContain("2026");
    // The empty state must NOT show when alerts exist.
    expect(screen.queryByText(/No alerts yet/)).toBeNull();
  });

  it("labels a first-version alert as New", async () => {
    watchLatest.mockResolvedValue({
      count: 1,
      alerts: [makeAlert({ diff_summary: "Initial version ingested. Begins: ..." })],
    });
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText("New")).toBeInTheDocument();
    expect(screen.queryByText("Revised")).toBeNull();
  });

  it("shows the empty state when the feed is loaded-but-empty", async () => {
    watchLatest.mockResolvedValue({ count: 0, alerts: [] });
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
    watchLatest.mockResolvedValue({ count: 0, alerts: [] });
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
    watchLatest.mockResolvedValue({ count: 0, alerts: [] });
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

  it("labels a null-summary alert as Revised (the wire shape for a summary-less row)", async () => {
    watchLatest.mockResolvedValue({ count: 1, alerts: [makeAlert({ diff_summary: null })] });
    listProducts.mockResolvedValue(NO_PRODUCTS);

    render(<WatchPage />);

    expect(await screen.findByText("Revised")).toBeInTheDocument();
    expect(screen.queryByText("New")).toBeNull();
  });

  it("renders nothing (not a wrong value) for a 0 confidence and an unparseable timestamp", async () => {
    watchLatest.mockResolvedValue({
      count: 1,
      alerts: [makeAlert({ confidence: 0, captured_at: "not-a-date" })],
    });
    listProducts.mockResolvedValue(NO_PRODUCTS);

    const { container } = render(<WatchPage />);
    await screen.findByText("Albuterol Sulfate");

    expect(screen.queryByText(/% match/)).toBeNull(); // not "0% match"
    // The detected-date line is absent: assert on the <time> element, not the
    // word "detected" (which also appears in the page tagline copy).
    expect(container.querySelector("time")).toBeNull(); // not "Invalid Date"
  });
});
