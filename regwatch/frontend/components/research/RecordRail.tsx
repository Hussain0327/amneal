"use client";

// The record rail: three panel toggles, icon over a label duplicating the
// accessible name. Left is what you own, centre is the artifact, and this is
// the third thing -- the record the artifact is made out of, plus the two ways
// of interrogating it.
//
// Still deliberately an unlabelled container rather than a nav or a toolbar,
// and the argument the Compliance Studio's ActivityRail makes still holds
// unchanged at three buttons: a landmark would file panel toggles under
// navigation, and role="toolbar" promises arrow-key roving this rail does not
// implement. Three plainly named buttons in tab order are the honest version.
//
// One deliberate departure from ActivityRail: every visible label here is a
// substring of its button's accessible name. ActivityRail labels a button
// "Compliance results" under the visible word "Findings", which leaves a
// speech-input user with no way to say the control's name (WCAG 2.5.3).

import { HistoryIcon } from "@/components/research/icons";
import { BookIcon, ChatIcon } from "@/components/studio/icons";
import type { RecordPanelId } from "@/lib/research-types";

interface RecordToggle {
  readonly id: RecordPanelId;
  /** Shown under the icon; must appear inside `name`. */
  readonly label: string;
  /** The accessible name. Says what the panel IS, not what clicking does --
   * aria-pressed already carries the open/closed half. */
  readonly name: string;
  readonly Icon: React.ComponentType<{ readonly size?: number; readonly className?: string }>;
}

const TOGGLES: readonly RecordToggle[] = [
  { id: "record", label: "Record", name: "Record, the FDA source corpus", Icon: BookIcon },
  { id: "assistant", label: "Assistant", name: "Assistant, ask about this artifact", Icon: ChatIcon },
  { id: "history", label: "History", name: "History of this artifact", Icon: HistoryIcon },
];

interface RecordRailProps {
  /** The open panel, or null when the record is closed -- which is the default:
   * the artifact is the work, the record is the lookup. */
  readonly panel: RecordPanelId | null;
  onTogglePanel: (id: RecordPanelId) => void;
}

export function RecordRail({ panel, onTogglePanel }: RecordRailProps): React.ReactElement {
  return (
    <div className="rw-rail">
      {TOGGLES.map(({ id, label, name, Icon }) => (
        <button
          key={id}
          type="button"
          className={`rw-rail__btn${panel === id ? " is-on" : ""}`}
          aria-pressed={panel === id}
          aria-label={name}
          onClick={() => onTogglePanel(id)}
        >
          <span className="rw-rail__icon">
            <Icon size={15} />
          </span>
          {/* Decorative duplicate of the aria-label above; the button's own
              aria-label is the accessible name and must not change. */}
          <span className="rw-rail__label" aria-hidden="true">
            {label}
          </span>
        </button>
      ))}
    </div>
  );
}
