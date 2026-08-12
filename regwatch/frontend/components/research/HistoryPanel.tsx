"use client";

// HISTORY: the artifact's audit trail.
//
// Not a second transcript. The transcript is the argument; this is the ledger
// of how the argument was arrived at -- every question, how it settled, what
// answered it, and the audit row it was written to. It is the panel an analyst
// opens six weeks later when somebody asks where a number in a filing came
// from, so it keeps the turns that produced NOTHING as carefully as the ones
// that produced an answer. A trail that quietly drops its refusals is not a
// trail, it is a highlight reel.
//
// Transcript order, oldest first, matching the sheet and matching the record
// drawer beside it. A row is a way back to its turn.

import { PanelEmpty, PanelFrame } from "@/components/research/PanelFrame";
import type { HistoryEntry } from "@/lib/research-record";
import type { ArtifactKind } from "@/lib/research-types";
import { formatClock, formatFiled } from "@/lib/time";

/** What the trail can honestly say when an artifact has filed nothing. */
const NOTHING_FILED: Record<ArtifactKind, { head: string; line: string }> = {
  thread: {
    head: "Nothing filed yet",
    line: "Every question put to this thread lands here with how it settled, what answered it, and its audit row.",
  },
  dossier: {
    head: "Not kept yet",
    line: "A dossier is composed on request and written nowhere, so there is no trail to keep. It gets one when it is saved.",
  },
  bulletin: {
    head: "Not kept here",
    line: "A bulletin IS a change record -- what a guidance was, and what it became. It moves here when bulletins get a sheet.",
  },
  paper: {
    head: "Not kept here",
    line: "A paper keeps its own run trail on its surface: inputs saved, cells verified, the run finalized or reopened.",
  },
};

interface HistoryPanelProps {
  readonly kind: ArtifactKind;
  readonly entries: readonly HistoryEntry[];
  /** Scroll the transcript to a turn. */
  readonly onJump: (key: string) => void;
  readonly onClose: () => void;
}

/**
 * The sources line for one entry.
 *
 * Only a turn that MAY carry grounding gets one. Printing "0 sources" beside a
 * clarify or a refusal states that grounding was expected and missing, when in
 * fact none was ever owed -- which is the INV-2 boundary drawn in the wrong
 * place.
 */
function SourceLine({ entry }: { readonly entry: HistoryEntry }): React.JSX.Element | null {
  if (entry.tone !== "answer") return null;
  if (entry.sourceCount === 0) {
    return <span className="rw-trail__src rw-trail__src--none">Not sourced</span>;
  }
  return (
    <span className="rw-trail__src">
      {entry.sourceCount} {entry.sourceCount === 1 ? "source" : "sources"}
    </span>
  );
}

export function HistoryPanel({
  kind,
  entries,
  onJump,
  onClose,
}: HistoryPanelProps): React.JSX.Element {
  const empty = NOTHING_FILED[kind];

  return (
    <PanelFrame
      label="History"
      onClose={onClose}
      context={
        <p className="rw-panel__ctx">
          Every question put to this artifact, how it settled, and what settled it.
        </p>
      }
    >
      {entries.length === 0 ? (
        <PanelEmpty head={empty.head} line={empty.line} />
      ) : (
        // An ordered list because the order IS the content here: this is a
        // sequence of events, which is the one case where numbering encodes
        // something the reader needs rather than decorating a list.
        <ol className="rw-trail">
          {entries.map((entry) => (
            <li className="rw-trail__item" key={entry.key}>
              <button
                type="button"
                className="rw-trail__row"
                onClick={() => onJump(entry.key)}
                aria-label={`${entry.outcome}. Go to the turn that asked: ${
                  entry.question || "this question"
                }`}
              >
                {/* The gutter stamp is a plain time, deliberately NOT the
                    bounded gold of a citation: a clock is not evidence, and
                    this system spends gold on grounding only. The full stamp
                    goes in the title so the day is reachable without leaving. */}
                <span
                  className="rw-trail__at"
                  aria-hidden="true"
                  title={entry.askedAt === null ? undefined : formatFiled(entry.askedAt)}
                >
                  {entry.askedAt === null ? "—" : formatClock(entry.askedAt)}
                </span>

                <span className="rw-trail__body">
                  <span className="rw-trail__q">{entry.question || "Question not recorded"}</span>

                  <span className="rw-trail__marks">
                    <span className={`rw-out rw-out--${entry.tone}`}>{entry.outcome}</span>
                    <SourceLine entry={entry} />
                  </span>

                  {/* Provenance, in the identifier face because that is exactly
                      what these are. Either half can be missing -- a rehydrated
                      turn keeps its audit id but not the model that wrote it --
                      so the line renders whichever half exists and nothing when
                      neither does. */}
                  {(entry.modelName !== null || entry.auditId !== null) && (
                    <span className="rw-trail__prov">
                      {entry.modelName !== null && (
                        <span className="rw-trail__model">{entry.modelName}</span>
                      )}
                      {entry.auditId !== null && (
                        <span className="rw-trail__audit">
                          <span className="rw-sr">Audit row </span>#{entry.auditId}
                        </span>
                      )}
                    </span>
                  )}

                  {/* Two things that happened TO the answer rather than in it.
                      Both are the server's own signal, never inferred here, and
                      both are the kind of thing an analyst reconstructing a
                      filing has to be able to see. */}
                  {entry.fellBack && (
                    <span className="rw-trail__flag">
                      Streaming failed; the answer was re-fetched.
                    </span>
                  )}
                  {entry.draftWithdrawn !== null && (
                    <span className="rw-trail__flag">
                      A provisional draft was withdrawn ({entry.draftWithdrawn}).
                    </span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </PanelFrame>
  );
}
