"use client";

// The activity rail: two panel toggles, icon over a label duplicating the
// accessible name. The repository-wide run now lives at the foot of the
// tree, so the rail carries only the two toggles it has always been about.
//
// Deliberately an unlabelled container rather than a nav or a toolbar. A
// landmark would file panel toggles under navigation, and role="toolbar"
// promises arrow-key roving this rail does not implement. Two plainly named
// buttons in tab order are the honest version.

import { ChatIcon, ShieldIcon } from "@/components/studio/icons";
import type { PanelId } from "@/lib/studio-types";

interface ActivityRailProps {
  panel: PanelId | null;
  /** Open, non-info findings on the active document. Drives the badge. */
  findingCount: number;
  checking: boolean;
  onTogglePanel: (id: PanelId) => void;
}

export function ActivityRail({ panel, findingCount, checking, onTogglePanel }: ActivityRailProps) {
  // The badge is a visual count only, so the same number has to be spoken as
  // part of the button's name or a screen reader hears an unqualified icon.
  const findingsLabel =
    findingCount > 0
      ? `Compliance results, ${findingCount} open ${findingCount === 1 ? "finding" : "findings"}`
      : "Compliance results";

  return (
    <div className="st-rail">
      <button
        type="button"
        className={`st-rail__btn${panel === "assistant" ? " is-on" : ""}`}
        aria-pressed={panel === "assistant"}
        aria-label="Ask about this document"
        onClick={() => onTogglePanel("assistant")}
      >
        <span className="st-rail__icon">
          <ChatIcon />
        </span>
        {/* Decorative duplicate of the aria-label above; the button's own
            aria-label (which folds in the finding count) is the accessible
            name and must not change. */}
        <span className="st-rail__label" aria-hidden="true">
          Ask
        </span>
      </button>

      <button
        type="button"
        className={`st-rail__btn${panel === "findings" ? " is-on" : ""}${checking ? " is-checking" : ""}`}
        aria-pressed={panel === "findings"}
        aria-label={findingsLabel}
        onClick={() => onTogglePanel("findings")}
      >
        <span className="st-rail__icon">
          <ShieldIcon />
          {findingCount > 0 && (
            <span className="st-rail__count" aria-hidden="true">
              {findingCount}
            </span>
          )}
        </span>
        <span className="st-rail__label" aria-hidden="true">
          Findings
        </span>
      </button>
    </div>
  );
}
