"use client";

// The bar above the page.
//
// It carries only controls that do real work against the plain-text block model:
// clearing the analyst's own highlights, and the tracked-changes switch.
// Character formatting (bold, italic, lists, tables) needs an editor engine this
// cut does not have, so it is absent rather than present and dead - a disabled
// B/I/U row would promise an editor that is not there.

import { HighlightIcon } from "@/components/studio/icons";

interface FormatBarProps {
  tracked: boolean;
  onTrackedChange: (v: boolean) => void;
  /** False when the document carries no highlights, so there is nothing to clear. */
  canClear: boolean;
  onClearHighlights: () => void;
}

export function FormatBar({ tracked, onTrackedChange, canClear, onClearHighlights }: FormatBarProps) {
  return (
    <div className="st-bar" role="toolbar" aria-label="Document actions">
      {/* An empty toolbar teaches nothing. This line names the one gesture the
          page depends on, which is otherwise only discoverable by accident. */}
      <span className="st-bar__hint">
        <HighlightIcon />
        Select any text to highlight it, or ask the assistant about it
      </span>

      <button type="button" className="st-bar__btn" onClick={onClearHighlights} disabled={!canClear}>
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
