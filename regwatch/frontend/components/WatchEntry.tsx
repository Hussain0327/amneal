"use client";

import type { AlertRecord } from "@/lib/api";
import { safeHref } from "@/lib/url";

function str(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

// Store timestamps (alert captured_at, run started/finished) are naive UTC, so
// a missing offset is treated as UTC -- the same convention Sidebar / White
// Paper timestamps use. Returns NaN when unparseable so callers can refuse to
// claim anything (a date, staleness) they cannot actually prove.
export function parseUtcMs(iso: string): number {
  if (!iso) return NaN;
  const norm = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
  return Date.parse(norm);
}

// timeZone is pinned so the rendered date is stable across machines/CI rather
// than shifting with the runner's locale.
export function fmtDetected(iso: string): string {
  const t = parseUtcMs(iso);
  if (Number.isNaN(t)) return "";
  return new Date(t).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

// Confidence is a 0..1 match score; show a whole percent, not the raw float that
// leaked before (0.9047619047619048). 0/NaN/undefined render nothing -- the
// matcher never emits a 0 score, so an absent one is a non-result, not "0%".
function pctMatch(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return "";
  return `${Math.round(v * 100)}% match`;
}

// New vs Revised prefers the backend's structural change_kind (derived from
// version history, so it survives the prod degrade where a revision's summary
// falls back to the "Initial version ingested" marker because the prior parsed
// text lived on an ephemeral cron runner). The prose-marker heuristic remains
// only as a fallback for alerts serialized before the field shipped: a first
// version carries the marker (change_detector.summarize_change); anything else
// is a revision. Match the ASCII prefix only (the marker's trailing excerpt is
// non-ASCII); a null/empty summary reads as Revised.
export function alertKind(r: Pick<AlertRecord, "change_kind" | "diff_summary">): "New" | "Revised" {
  if (r.change_kind === "new") return "New";
  if (r.change_kind === "revised") return "Revised";
  return (r.diff_summary ?? "").trim().startsWith("Initial version ingested") ? "New" : "Revised";
}

// One bulletin entry: a docket margin on the left (kind stamp, detected date,
// match score) beside the entry body. Only a NEW entry carries the gold spine
// -- on this surface gold marks arrival, a revision keeps the hollow ring.
export function AlertEntry({
  alert,
  scopeable,
  scoped,
  onScope,
}: {
  alert: AlertRecord;
  scopeable: boolean;
  scoped: boolean;
  onScope: () => void;
}) {
  const kind = alertKind(alert);
  const name = str(alert.active_ingredient);
  const applNo = str(alert.listing_appl_no);
  const psgType = str(alert.listing_psg_type);
  const summary = str(alert.diff_summary);
  const sourceUrl = str(alert.source_url);
  const captured = str(alert.captured_at);
  const when = fmtDetected(captured);
  const pct = pctMatch(alert.confidence);
  return (
    <article className={kind === "New" ? "entry entry--new" : "entry"}>
      <div className="entry__rail">
        <span className={kind === "New" ? "entry__kind entry__kind--new" : "entry__kind"}>
          {kind}
        </span>
        {when && (
          <time className="entry__when" dateTime={captured} title="Date detected">
            {when}
          </time>
        )}
        {pct && <span className="entry__match">{pct}</span>}
      </div>
      <div className="entry__body">
        <div className="entry__top">
          <span className="display entry__name">{name || "—"}</span>
          <span className="entry__id code">
            PSG {applNo || "—"}
            {psgType && ` · ${psgType}`}
          </span>
          {scopeable && (
            <button className="chip entry__scope" type="button" aria-pressed={scoped} onClick={onScope}>
              {scoped ? "scoped" : "scope"}
            </button>
          )}
        </div>
        {summary && <p className="entry__sum">{summary}</p>}
        {sourceUrl && (
          <div className="entry__meta">
            <a className="link code" href={safeHref(sourceUrl)} target="_blank" rel="noreferrer">
              View source ↗
            </a>
          </div>
        )}
      </div>
    </article>
  );
}
