// Shared timestamp helpers for the Ask surface. The backend emits naive-UTC
// timestamps (no offset) from Postgres; browsers would otherwise parse
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

/**
 * Bucket label for the history docket's day groups, computed in the viewer's
 * LOCAL calendar (the wire is naive-UTC; grouping by the raw string would
 * bucket by UTC day and mislabel evening sessions). Buckets, newest first:
 * "Today", "Yesterday", "This week" (last 7 days), then month labels like
 * "July 2026" (en-US pinned for deterministic tests). Returns "Earlier" when
 * the timestamp does not parse -- an unparseable date is still a session the
 * analyst must be able to reach.
 */
/**
 * Compact relative age ("now", "42m", "6h", "6d", then a date) for run rows and
 * analyst attribution. Same naive-UTC convention as everything else here.
 * Returns "" when the timestamp does not parse.
 */
export function relTime(iso: string): string {
  const t = parseApiDate(iso);
  if (t === null) return "";
  const mins = Math.floor(Math.max(0, Date.now() - t) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d`;
  return new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/**
 * Absolute "Jul 1, 2026, 10:00 AM" stamp for the document's own furniture
 * (data-as-of, finalized-at). en-US pinned so the rendering is deterministic
 * under vitest. Returns the raw string when it does not parse -- an unreadable
 * timestamp is still evidence of when something happened.
 */
export function formatWhen(iso: string): string {
  const t = parseApiDate(iso);
  if (t === null) return iso;
  return new Date(t).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function historyBucket(iso: string, nowMs: number): string {
  const t = parseApiDate(iso);
  if (t === null) return "Earlier";
  const now = new Date(nowMs);
  const then = new Date(t);
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(then)) / 86_400_000);
  if (dayDiff <= 0) return "Today";
  if (dayDiff === 1) return "Yesterday";
  if (dayDiff < 7) return "This week";
  return then.toLocaleDateString("en-US", { month: "long", year: "numeric" });
}
