"use client";

import type { CSSProperties } from "react";

import { ChatIcon, HighlightIcon } from "@/components/studio/icons";
import type { SelectionAction } from "@/lib/studio-types";

/**
 * A live text selection inside one block. Offsets are into that block's plain
 * text, so an action can be replayed against the model; the rect is in viewport
 * coordinates because the toolbar is fixed, not laid out in the page.
 */
export interface StudioSelection {
  blockId: string;
  start: number;
  end: number;
  rect: { top: number; left: number; width: number };
}

// Toolbar height plus a gap. It sits above the selection, and flips below when
// there is not a toolbar's worth of room at the top of the viewport.
const GAP_ABOVE = 44;
const GAP_BELOW = 28;
// Half the widest the bar renders at: a centred bar clamped to this keeps both
// ends on screen when the selection is near either edge.
const EDGE = 90;

interface Props {
  selection: StudioSelection | null;
  onAction: (a: SelectionAction) => void;
}

export function SelectionToolbar({ selection, onAction }: Props) {
  if (!selection) return null;

  const { rect } = selection;
  const above = rect.top - GAP_ABOVE;
  const viewport = typeof window === "undefined" ? EDGE * 2 : window.innerWidth;
  const centre = rect.left + rect.width / 2;

  const placement: CSSProperties = {
    top: above < 0 ? rect.top + GAP_BELOW : above,
    left: Math.min(Math.max(centre, EDGE), Math.max(EDGE, viewport - EDGE)),
    transform: "translateX(-50%)",
  };

  return (
    <div
      className="st-sel"
      role="toolbar"
      aria-label="Actions for the selected text"
      style={placement}
      // Mousing down outside a range collapses it, and every action here needs
      // the offsets that range carries. Suppressing the default keeps the
      // selection alive long enough for the click handler to read it.
      onMouseDown={(event) => event.preventDefault()}
    >
      <button
        type="button"
        className="st-sel__btn st-sel__btn--mark"
        onClick={() => onAction("highlight")}
      >
        <HighlightIcon />
        Highlight
      </button>
      <span className="st-sel__sep" aria-hidden="true" />
      <button type="button" className="st-sel__btn" onClick={() => onAction("summarize")}>
        Summarize
      </button>
      <button type="button" className="st-sel__btn" onClick={() => onAction("explain")}>
        Explain
      </button>
      <button type="button" className="st-sel__btn" onClick={() => onAction("check")}>
        Check
      </button>
      <span className="st-sel__sep" aria-hidden="true" />
      <button type="button" className="st-sel__btn" onClick={() => onAction("ask")}>
        <ChatIcon />
        Ask
      </button>
    </div>
  );
}
