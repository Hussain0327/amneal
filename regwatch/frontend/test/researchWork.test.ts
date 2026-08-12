// The work-list boundary: what the rail is allowed to believe about a count.
//
// `total` and the rows behind it are two queries on the server, so the pair can
// arrive disagreeing, and getJSON casts rather than validates -- a payload can
// omit the field outright. Both land here, and the rail heads a group with
// whatever this returns, so this is where it has to be made safe to render.
import { describe, expect, it, vi } from "vitest";

import type { WatchLatest, WhitepaperRunList } from "@/lib/api";

const watchLatestMock = vi.fn<() => Promise<WatchLatest>>();
const listWhitepaperRunsMock = vi.fn<() => Promise<WhitepaperRunList>>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    watchLatest: () => watchLatestMock(),
    listWhitepaperRuns: () => listWhitepaperRunsMock(),
    listSessions: async () => ({ sessions: [] }),
  };
});

const { fetchKindGroup } = await import("@/lib/research-work");

function alert(id: number): WatchLatest["alerts"][number] {
  return {
    active_ingredient: "Albuterol sulfate",
    captured_at: "2026-08-12T00:00:00Z",
    listing_appl_no: "020503",
    psg_document_id: id,
    psg_version_id: id,
  } as WatchLatest["alerts"][number];
}

function run(id: number): WhitepaperRunList["runs"][number] {
  return {
    application_number: "020503",
    id,
    ingredient: "Ibuprofen",
    rld_name_input: "Motrin",
    updated_at: "2026-08-12T00:00:00Z",
  } as WhitepaperRunList["runs"][number];
}

describe("a count is never smaller than the page it came with", () => {
  it("floors a short bulletin total at the rows actually sent", async () => {
    // A COUNT racing an INSERT comes back behind the list. Rendered raw, the
    // rail would head a visibly non-empty group with 0.
    watchLatestMock.mockResolvedValue({
      alerts: [alert(1), alert(2)],
      count: 2,
      last_run: null,
      limit: 50,
      offset: 0,
      total: 0,
    });

    const group = await fetchKindGroup("bulletin", new AbortController().signal);
    expect(group.items).toHaveLength(2);
    expect(group.total).toBe(2);
  });

  it("falls back to the page when the payload omits the total", async () => {
    // getJSON casts, so a missing field is a runtime value the type does not
    // catch. "Papers, undefined" is not a count.
    listWhitepaperRunsMock.mockResolvedValue({
      count: 1,
      limit: 50,
      offset: 0,
      runs: [run(7)],
    } as unknown as WhitepaperRunList);

    const group = await fetchKindGroup("paper", new AbortController().signal);
    expect(group.total).toBe(1);
  });

  it("keeps a larger total, which is the paged case it exists for", async () => {
    listWhitepaperRunsMock.mockResolvedValue({
      count: 1,
      limit: 50,
      offset: 0,
      runs: [run(7)],
      total: 214,
    });

    const group = await fetchKindGroup("paper", new AbortController().signal);
    expect(group.total).toBe(214);
  });

  it("a failed kind reports no count at all, not a zero", async () => {
    watchLatestMock.mockRejectedValue(new Error("nope"));

    const group = await fetchKindGroup("bulletin", new AbortController().signal);
    expect(group.state).toBe("unreachable");
    expect(group.total).toBe(0);
    expect(group.items).toEqual([]);
  });
});
