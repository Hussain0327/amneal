"use client";

import { BookIcon, ChatIcon, CloseIcon, PinIcon, ShieldIcon } from "@/components/studio/icons";
import { sortFindings, verdictFor } from "@/lib/studio-marks";
import type { Finding, StudioDoc } from "@/lib/studio-types";

interface FindingsPanelProps {
  doc: StudioDoc;
  activeFindingId: string | null;
  onSelect: (id: string) => void;
  onAsk: (f: Finding) => void;
  onClose: () => void;
}

/**
 * The verdict counts. "blocking", "to resolve" and "stale" are adjectival here
 * and never take a plural s; only "note" is a countable noun.
 */
function countLabel(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

export function FindingsPanel({ doc, activeFindingId, onSelect, onAsk, onClose }: FindingsPanelProps) {
  const v = verdictFor(doc);
  const findings = sortFindings(doc);
  const against = doc.standards.join(", ");

  return (
    <>
      <div className="st-panel__head">
        <span className="st-panel__title">
          <ShieldIcon />
          Compliance results
        </span>
        <button type="button" className="st-icon-btn st-panel__close" onClick={onClose} aria-label="Close compliance results">
          <CloseIcon />
        </button>
      </div>

      <div className="st-panel__scroll">
        {doc.checkState === "checking" ? (
          <p className="st-panel__empty">
            Checking <b>{doc.name}</b> against {against}...
          </p>
        ) : doc.checkState === "unchecked" ? (
          <p className="st-panel__empty">
            <b>{doc.name}</b> has not been checked yet. Run Check this document in the repository panel, or run the whole
            repository from the right rail.
          </p>
        ) : (
          <>
            <div className={`st-verdict st-verdict--${v.tone}`}>
              <div className="st-verdict__label">
                <ShieldIcon />
                {v.label}
              </div>
              <div className="st-verdict__counts">
                {v.blocking > 0 && <span className="st-count st-count--blocking">{v.blocking} blocking</span>}
                <span className="st-count">{v.toResolve} to resolve</span>
                {v.notes > 0 && <span className="st-count">{countLabel(v.notes, "note")}</span>}
                {v.stale > 0 && <span className="st-count">{v.stale} stale</span>}
              </div>
              <p className="st-verdict__against">Checked against {against}.</p>
            </div>

            {v.tone === "clear" && doc.findings.length === 0 && (
              <p className="st-panel__empty">Nothing to resolve. Re-run the check after any edit.</p>
            )}

            {findings.length > 0 && (
              <ul className="st-find__list">
                {findings.map((f) => (
                  <li
                    key={f.id}
                    className={`st-find st-find--${f.severity}${f.stale ? " is-stale" : ""}${
                      f.id === activeFindingId ? " is-active" : ""
                    }`}
                  >
                    <button
                      type="button"
                      className="st-find__main"
                      onClick={() => onSelect(f.id)}
                      aria-label={`Show ${f.title} in the document`}
                      aria-current={f.id === activeFindingId ? "true" : undefined}
                    >
                      <div className="st-find__head">
                        <span className={`st-sev st-sev--${f.severity}`}>{f.severity.toUpperCase()}</span>
                        <span className="st-find__title">{f.title}</span>
                      </div>
                      <p className="st-find__detail">{f.detail}</p>
                      <div className="st-find__meta">
                        <span>
                          <PinIcon />
                          {f.location}
                        </span>
                        <span>
                          <BookIcon />
                          {f.standard}
                        </span>
                      </div>
                    </button>

                    {f.stale && <span className="st-find__staletag">Text edited - recheck</span>}

                    <button type="button" className="st-find__ask" onClick={() => onAsk(f)}>
                      <ChatIcon size={12} />
                      Explain the rule
                      {/* Every card carries this button; the hidden tail is what tells them apart in a screen reader's list. */}
                      <span className="studio__sr"> behind {f.title}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </>
  );
}
