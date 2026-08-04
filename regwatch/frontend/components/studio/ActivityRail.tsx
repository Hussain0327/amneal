"use client";

// The activity rail: two panel toggles at the head, the repository-wide run
// pinned at the foot.
//
// Deliberately an unlabelled container rather than a nav or a toolbar. A
// landmark would file a run-every-document action under navigation, and
// role="toolbar" promises arrow-key roving this rail does not implement. Three
// plainly named buttons in tab order are the honest version.

import { ChatIcon, ShieldIcon } from "@/components/studio/icons";
import type { PanelId } from "@/lib/studio-types";

interface ActivityRailProps {
  panel: PanelId | null;
  /** Open, non-info findings on the active document. Drives the badge. */
  findingCount: number;
  checking: boolean;
  onTogglePanel: (id: PanelId) => void;
  onRunFullCheck: () => void;
}

export function ActivityRail({
  panel,
  findingCount,
  checking,
  onTogglePanel,
  onRunFullCheck,
}: ActivityRailProps) {
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
        <ChatIcon />
      </button>

      <button
        type="button"
        className={`st-rail__btn${panel === "findings" ? " is-on" : ""}`}
        aria-pressed={panel === "findings"}
        aria-label={findingsLabel}
        onClick={() => onTogglePanel("findings")}
      >
        <ShieldIcon />
        {findingCount > 0 && (
          <span className="st-rail__count" aria-hidden="true">
            {findingCount}
          </span>
        )}
      </button>

      <button type="button" className="st-rail__full" onClick={onRunFullCheck} disabled={checking}>
        {checking ? "Checking" : "Run full check"}
      </button>
    </div>
  );
}
