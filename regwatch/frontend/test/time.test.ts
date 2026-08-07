// lib/time.ts unit tests. Expected clock values are derived from the SAME
// Date the helpers see (local getters), so the assertions hold in any host
// timezone while still failing if the helpers stopped rendering local,
// zero-padded, 24-hour time.
import { describe, expect, it } from "vitest";

import { formatClock, formatFiled, historyBucket, parseApiDate } from "@/lib/time";

const ISO_UTC = "2026-01-07T14:32:00Z";

function expectedClock(iso: string): string {
  const d = new Date(Date.parse(iso));
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

describe("parseApiDate", () => {
  it("parses an explicit-UTC timestamp to epoch millis", () => {
    expect(parseApiDate(ISO_UTC)).toBe(Date.parse(ISO_UTC));
  });

  it("treats an offsetless timestamp as UTC (the naive-UTC wire rule)", () => {
    expect(parseApiDate("2026-01-07T14:32:00")).toBe(Date.parse(ISO_UTC));
  });

  it("respects an explicit offset", () => {
    expect(parseApiDate("2026-01-07T14:32:00+02:00")).toBe(Date.parse("2026-01-07T12:32:00Z"));
  });

  it("returns null when the string does not parse", () => {
    expect(parseApiDate("not a date")).toBeNull();
  });
});

describe("formatClock", () => {
  it("renders the local 24-hour wall clock, zero-padded", () => {
    expect(formatClock(ISO_UTC)).toBe(expectedClock(ISO_UTC));
  });

  it("applies the naive-UTC rule to offsetless input", () => {
    expect(formatClock("2026-01-07T14:32:00")).toBe(formatClock(ISO_UTC));
  });

  it("returns an empty string on unparseable input", () => {
    expect(formatClock("garbage")).toBe("");
  });
});

describe("formatFiled", () => {
  it("renders 'Mon D, YYYY \\u00b7 HH:MM' with no AM/PM marker", () => {
    // \u00b7 inside the regex matches the literal middle dot separator.
    expect(formatFiled(ISO_UTC)).toMatch(/^[A-Z][a-z]{2} \d{1,2}, \d{4} \u00b7 \d{2}:\d{2}$/);
  });

  it("agrees with formatClock (provenance and margin can never disagree)", () => {
    expect(formatFiled(ISO_UTC).endsWith(formatClock(ISO_UTC))).toBe(true);
  });

  it("applies the naive-UTC rule to offsetless input", () => {
    expect(formatFiled("2026-01-07T14:32:00")).toBe(formatFiled(ISO_UTC));
  });

  it("returns an empty string on unparseable input", () => {
    expect(formatFiled("garbage")).toBe("");
  });
});

describe("historyBucket", () => {
  // A fixed "now": Friday Aug 7, 2026 15:00 UTC (tests run under TZ=UTC).
  const NOW = Date.parse("2026-08-07T15:00:00Z");

  it("buckets the same local day as Today", () => {
    expect(historyBucket("2026-08-07T01:10:00Z", NOW)).toBe("Today");
  });

  it("buckets the previous local day as Yesterday", () => {
    expect(historyBucket("2026-08-06T23:59:00Z", NOW)).toBe("Yesterday");
  });

  it("buckets 2-6 days back as This week", () => {
    expect(historyBucket("2026-08-04T09:00:00Z", NOW)).toBe("This week");
    expect(historyBucket("2026-08-01T09:00:00Z", NOW)).toBe("This week");
  });

  it("labels older sessions by month and year", () => {
    expect(historyBucket("2026-07-18T09:00:00Z", NOW)).toBe("July 2026");
    expect(historyBucket("2025-12-30T09:00:00Z", NOW)).toBe("December 2025");
  });

  it("files an unparseable timestamp under Earlier (still reachable)", () => {
    expect(historyBucket("not-a-date", NOW)).toBe("Earlier");
  });

  it("clamps clock skew (a future timestamp) to Today", () => {
    expect(historyBucket("2026-08-08T00:30:00Z", NOW)).toBe("Today");
  });
});
