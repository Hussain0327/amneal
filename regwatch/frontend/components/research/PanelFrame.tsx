"use client";

// The drawer the three record panels open into.
//
// One frame rather than three, because the three panels are three cards in one
// index and not three tools: same head, same rule, same scroller, same close.
// Anything that differed between them would read as having been built by
// somebody else on a different day.
//
// The frame owns NO data and no fetching. It is the head, the scroller and the
// optional docked foot; each panel fills it. That is the whole abstraction, and
// it exists because there are three concrete callers today, not because there
// might be a fourth.

import { CloseIcon } from "@/components/studio/icons";
import type { ReactNode } from "react";

interface PanelFrameProps {
  /** "Record" | "Assistant" | "History". Also the close button's name. */
  readonly label: string;
  /**
   * The one line that says what this panel is looking at, drawn between the
   * head and the scroller. Optional: History describes itself.
   */
  readonly context?: ReactNode;
  /** Docked below the scroller, on the frame's own ground. */
  readonly foot?: ReactNode;
  readonly onClose: () => void;
  readonly children: ReactNode;
}

export function PanelFrame({
  label,
  context,
  foot,
  onClose,
  children,
}: PanelFrameProps): React.JSX.Element {
  return (
    // Labelled by the same word the rail's toggle shows, so the region a
    // screen reader lands in is the one the analyst thinks they opened.
    <aside className="rw-panel" aria-label={`${label} panel`}>
      <div className="rw-panel__head">
        <p className="rw-eyebrow rw-panel__title">{label}</p>
        {/* The rail's toggle already closes this. The button is here anyway:
            the toggle is 24rem away past the panel's own content, and a
            keyboard user who has tabbed into the panel should not have to tab
            back out of it to leave. Its name carries the visible word (2.5.3). */}
        <button
          type="button"
          className="rw-icon-btn rw-panel__close"
          onClick={onClose}
          aria-label={`Close ${label}`}
        >
          <CloseIcon />
        </button>
      </div>

      {context !== undefined && <div className="rw-panel__context">{context}</div>}

      <div className="rw-panel__scroll">{children}</div>

      {foot !== undefined && <div className="rw-panel__foot">{foot}</div>}
    </aside>
  );
}

/**
 * The drawer's empty state.
 *
 * A shared component and not a shared string: all three panels can be empty for
 * reasons that are NOT interchangeable ("nothing yet" and "nothing here" are
 * opposite claims), so the frame fixes the shape and every caller writes its
 * own sentence.
 */
interface PanelEmptyProps {
  /** What is not there. One short phrase, sentence case. */
  readonly head: string;
  /** Why, and what to do about it. */
  readonly line: string;
}

export function PanelEmpty({ head, line }: PanelEmptyProps): React.JSX.Element {
  return (
    <div className="rw-empty">
      <p className="rw-empty__head">{head}</p>
      <p className="rw-empty__line">{line}</p>
    </div>
  );
}
