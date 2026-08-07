// Shared timestamp helpers for the Ask surface. The backend emits naive-UTC
// timestamps (no offset) from SQLite/Postgres; browsers would otherwise parse
// those as LOCAL time and skew every displayed date by the viewer's offset.
// Same rule as the Sidebar's relTime: a missing offset means UTC.

/**
 * Parses an API timestamp to epoch milliseconds, treating an offset-less
 * string as UTC (the backend writes naive-UTC datetimes). Returns null when
 * the string does not parse, so callers can omit the affordance instead of
 * rendering "Invalid Date".
 */
export function parseApiDate(iso: string): number | null {
  const norm = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
  const t = Date.parse(norm);
  return Number.isNaN(t) ? null : t;
}

/**
 * Local wall-clock "HH:MM" (24-hour) for an API timestamp -- the docket
 * margin's compact time-filed. Locale is pinned to en-US and hour12 to false
 * so the rendering is deterministic under vitest regardless of host locale.
 * Returns "" when the timestamp does not parse.
 */
export function formatClock(iso: string): string {
  const t = parseApiDate(iso);
  if (t === null) return "";
  return new Date(t).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/**
 * Absolute "filed" stamp for provenance lines: "Jan 7, 2026 \u00b7 14:32" in
 * the viewer's timezone. Built on formatClock so the provenance date and the
 * docket-margin time can never disagree. Returns "" when the timestamp does
 * not parse.
 */
export function formatFiled(iso: string): string {
  const t = parseApiDate(iso);
  if (t === null) return "";
  const date = new Date(t).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  return `${date} \u00b7 ${formatClock(iso)}`;
}
