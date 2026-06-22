import type { Citation } from "@/lib/api";

// Display-only revision provenance for a cited source: the FDA recommended date
// of the guidance revision and, when the source carries a change note, what
// changed in it. Both ride on the citation wire (recommended_date / diff_summary
// / version_id) — this renders them, computes nothing. recommended_date is a
// raw string the backend already null-guards on bad input, so it is shown as-is.
//
// Nothing renders when neither a date nor a diff is present, so an older turn
// (or a streamed response that didn't carry recency) shows no empty badge.
export function RecencyBadge({ c }: { c: Citation }) {
  const date = c.recommended_date ?? null;
  const diff = c.diff_summary ?? null;
  if (!date && !diff) return null;
  return (
    <div className="recency">
      {date && (
        <span className="recency__rev">
          FDA recommended <time>{date}</time>
        </span>
      )}
      {diff && <span className="recency__diff">What changed: {diff}</span>}
    </div>
  );
}
