"use client";

// The bar above the page.
//
// It carries the controls that do real work against the plain-text block
// model -- clearing the analyst's own highlights, the tracked-changes switch
// -- and a live read of check status, which is what an analyst actually
// tracks while reading rather than a sentence teaching a gesture. Character
// formatting (bold, italic, lists, tables) needs an editor engine this cut
// does not have, so it is absent rather than present and dead - a disabled
// B/I/U row would promise an editor that is not there.

import type { CheckState } from "@/lib/studio-types";

interface FormatBarProps {
  tracked: boolean;
  onTrackedChange: (v: boolean) => void;
  /** False when the document carries no highlights, so there is nothing to clear. */
  canClear: boolean;
  onClearHighlights: () => void;
  checkState: CheckState;
  openCount: number;
  totalCount: number;
}

type Status =
  | { kind: "text"; text: string; tone: "clear" | null }
  | { kind: "count"; open: number; total: number };

// Exhaustive over CheckState: a future member fails the build here instead of
// leaving the bar blank.
function checkStatus(checkState: CheckState, openCount: number, totalCount: number): Status {
  switch (checkState) {
    case "unchecked":
      return { kind: "text", text: "Not checked", tone: null };
    case "checking":
      return { kind: "text", text: "Checking...", tone: null };
    case "stale":
      return { kind: "text", text: "Edited since last check", tone: null };
    case "checked":
      return openCount > 0
        ? { kind: "count", open: openCount, total: totalCount }
        : { kind: "text", text: "No open findings", tone: "clear" };
    default: {
      const exhaustive: never = checkState;
      return exhaustive;
    }
  }
}

export function FormatBar({
  tracked,
  onTrackedChange,
  canClear,
  onClearHighlights,
  checkState,
  openCount,
  totalCount,
}: FormatBarProps) {
  const status = checkStatus(checkState, openCount, totalCount);
  const tone = status.kind === "count" ? "flag" : status.tone;

  return (
    <div className="st-bar" role="toolbar" aria-label="Document actions">
      {/* Not a live region, deliberately. The studio mounts exactly one (see
          app/studio/page.tsx), and this bar changes on the same state
          transitions that region already speaks -- a check completing, a fix
          being applied -- so a second polite region here announced everything
          twice. The count is shown, not spoken; page.tsx speaks it. */}
      <span className={`st-bar__status${tone ? ` st-bar__status--${tone}` : ""}`}>
        {status.kind === "count" ? (
          <>
            <span className="st-bar__num">{status.open}</span> of{" "}
            <span className="st-bar__num">{status.total}</span> findings open
          </>
        ) : (
          status.text
        )}
      </span>

      <button type="button" className="st-btn st-btn--quiet" onClick={onClearHighlights} disabled={!canClear}>
        Clear highlights
      </button>

      <span className="st-bar__spacer" />

      <button
        type="button"
        className={`st-bar__toggle${tracked ? " is-on" : ""}`}
        aria-pressed={tracked}
        onClick={() => onTrackedChange(!tracked)}
      >
        <span className="st-bar__switch" />
        Tracked changes
      </button>
    </div>
  );
}
