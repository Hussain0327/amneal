"use client";

import { useEffect, useId, useRef } from "react";

import {
  ArrowIcon,
  BookIcon,
  ChatIcon,
  CheckIcon,
  CloseIcon,
  CopyIcon,
  PinIcon,
  ShieldIcon,
  UndoIcon,
} from "@/components/studio/icons";
import {
  canApplySuggestion,
  canDispose,
  currentRecord,
  isDisposed,
  recordVoid,
  sortFindings,
  verdictFor,
} from "@/lib/studio-marks";
import { JUSTIFICATION_MAX } from "@/lib/studio-types";
import type { Disposition, DispositionError, Finding, Severity, StudioDoc } from "@/lib/studio-types";

/** A disposition the analyst has picked but not yet recorded. */
export interface PendingDisposition {
  findingId: string;
  disposition: Disposition;
}

interface FindingsPanelProps {
  doc: StudioDoc;
  activeFindingId: string | null;
  pending: PendingDisposition | null;
  /** Half-typed justifications, keyed by finding id, so they survive a panel close. */
  drafts: Record<string, string>;
  error: { findingId: string; reason: DispositionError } | null;
  /** The record as text, when the clipboard refused it. Null on the happy path. */
  copyFallback: string | null;
  onSelect: (id: string) => void;
  onAsk: (f: Finding) => void;
  onClose: () => void;
  onApplySuggestion: (findingId: string) => void;
  onRevert: (blockId: string) => void;
  onPickDisposition: (f: Finding, disposition: Disposition) => void;
  onDraftChange: (findingId: string, text: string) => void;
  onRecord: (f: Finding) => void;
  onCancelPending: () => void;
  onCopyRecord: () => void;
  onStep: (step: 1 | -1) => void;
}

/** The four dispositions, in the order a reviewer reaches for them. */
const DISPOSITIONS: Disposition[] = ["fixed", "fixed_elsewhere", "not_applicable", "disputed"];

const DISPOSITION_LABEL: Record<Disposition, string> = {
  fixed: "Fixed",
  fixed_elsewhere: "Fixed elsewhere",
  not_applicable: "Not applicable",
  disputed: "Disputed",
};

/**
 * Four severities onto the three weights the severity chip carries. Critical
 * and major are both defects and both read solid; what separates them is the
 * word in the chip, the ring the critical one takes, and the blocking count in
 * the verdict. A fourth fill would turn the ladder into a rainbow.
 */
const SEVERITY_WEIGHT: Record<Severity, "major" | "minor" | "info"> = {
  critical: "major",
  major: "major",
  minor: "minor",
  info: "info",
};

/** What each disposition needs written down, and why. */
const JUSTIFICATION_PROMPT: Record<Exclude<Disposition, "fixed">, { label: string; help: string }> = {
  fixed_elsewhere: {
    label: "Where did you fix it?",
    help: "Required. Name the block, document, or change record that carries the fix.",
  },
  not_applicable: {
    label: "Why is this not applicable?",
    help: "Required. Name the condition that puts this finding out of scope, and the document or SOP that establishes it.",
  },
  disputed: {
    label: "What does the check have wrong?",
    help: "Required. Quote what the document says, and name the requirement you read it against.",
  },
};

/**
 * Every refusal says what happened and what to do about it. A disposition that
 * silently fails is how a reviewer stops trusting the record.
 */
function errorMessage(reason: DispositionError, disposition: Disposition, length: number): string {
  switch (reason) {
    case "fix_not_evidenced":
      return "Fixed needs a change to the text this finding points at. Apply the suggested fix, edit the block, or record Fixed elsewhere.";
    case "not_editable":
      return "This block is a table and cannot be edited here. Record Not applicable, Disputed, or Fixed elsewhere.";
    case "check_in_flight":
      return "Wait for the check to finish before you record a disposition.";
    case "unknown_finding":
      return "That finding is no longer on this document. Run the check again.";
    case "justification_too_long":
      return `Shorten the justification to ${JUSTIFICATION_MAX} characters or fewer. It is ${length} now.`;
    case "justification_required":
      return disposition === "fixed_elsewhere"
        ? "Enter a justification before you record this. Name the block, document, or change record that carries the fix."
        : disposition === "not_applicable"
          ? "Enter a justification before you record this. Name the condition that puts the finding out of scope and where it is documented."
          : "Enter a justification before you record this. Quote what the document says and name the requirement you read it against.";
  }
}

/**
 * The verdict counts. "blocking", "to resolve", "recorded" and "stale" are
 * adjectival here and never take a plural s; only "note" is a countable noun.
 */
function countLabel(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

// ---------------------------------------------------------------------------
// One finding
// ---------------------------------------------------------------------------

interface CardProps extends Pick<
  FindingsPanelProps,
  | "doc"
  | "pending"
  | "drafts"
  | "error"
  | "onSelect"
  | "onAsk"
  | "onApplySuggestion"
  | "onRevert"
  | "onPickDisposition"
  | "onDraftChange"
  | "onRecord"
  | "onCancelPending"
> {
  f: Finding;
  active: boolean;
}

function FindingCard({
  f,
  doc,
  active,
  pending,
  drafts,
  error,
  onSelect,
  onAsk,
  onApplySuggestion,
  onRevert,
  onPickDisposition,
  onDraftChange,
  onRecord,
  onCancelPending,
}: CardProps) {
  const uid = useId();
  const titleId = `${uid}-title`;
  const detailId = `${uid}-detail`;
  const gateId = `${uid}-gate`;
  const errorId = `${uid}-error`;
  const editorRef = useRef<HTMLTextAreaElement>(null);

  const block = doc.blocks.find((b) => b.id === f.blockId);
  const record = currentRecord(f);
  const disposed = isDisposed(f);
  const voided = recordVoid(block, f);
  const draft = drafts[f.id] ?? "";
  const cardError = error?.findingId === f.id ? error.reason : null;
  const editing = pending?.findingId === f.id ? pending.disposition : null;

  const applicable = canApplySuggestion(doc, f);
  const fixCheck = canDispose(doc, f, "fixed", "");
  const onTable = block?.type === "table";

  // Focus follows the disposition the analyst picked: the field is the only
  // announcement channel a required input gets before it has been submitted.
  useEffect(() => {
    if (editing && editing !== "fixed") editorRef.current?.focus();
  }, [editing]);

  const gate = onTable
    ? "This block is a table and cannot be edited here. Record Not applicable, Disputed, or Fixed elsewhere."
    : "Fixed opens once you change the text this finding points at. Apply the suggested fix, or edit the block yourself. If you fixed it somewhere else, use Fixed elsewhere.";

  return (
    <li
      className={`st-find st-find--${f.severity}${disposed ? " is-disposed" : ""}${
        f.stale && !disposed ? " is-stale" : ""
      }${active ? " is-active" : ""}`}
      data-finding-card={f.id}
      aria-labelledby={titleId}
      aria-describedby={detailId}
    >
      <div className="st-find__head">
        {/* The chip is a colour and a word; the word alone goes to a screen reader
            so the severity is not announced twice. */}
        <span
          className={`st-sev st-sev--${SEVERITY_WEIGHT[f.severity]}${
            f.severity === "critical" ? " st-find__sev--blocking" : ""
          }`}
          aria-hidden="true"
        >
          {f.severity.toUpperCase()}
        </span>
        <h3 className="st-find__title" id={titleId}>
          <span className="studio__sr">{f.severity} finding. </span>
          {f.title}
        </h3>
        {/* Both tools name the finding in their accessible name: an icon reached
            out of card order still has to say which finding it acts on. */}
        <div className="st-find__tools">
          <button
            type="button"
            className="st-icon-btn st-find__tool"
            title="Show in document"
            aria-label={`Show in document: ${f.title}`}
            onClick={() => onSelect(f.id)}
          >
            <PinIcon />
          </button>
          <button
            type="button"
            className="st-icon-btn st-find__tool"
            title="Explain the rule"
            aria-label={`Explain the rule behind ${f.title}`}
            onClick={() => onAsk(f)}
          >
            <ChatIcon />
          </button>
        </div>
      </div>

      <p className="st-find__detail" id={detailId}>
        {f.detail}
      </p>

      <div className="st-find__meta">
        <span className="st-chip">
          <PinIcon />
          {f.location}
        </span>
        <span className="st-chip">
          <BookIcon />
          {f.standard}
        </span>
      </div>

      {f.contested && (
        <p className="st-find__flag">
          The check ran again and still reports this. Your fix record is kept and the finding is open again.
        </p>
      )}
      {voided && (
        <p className="st-find__flag">
          The text is back to what the checker read. This fix record has no evidence behind it. Edit the text
          again, or change the disposition.
        </p>
      )}

      {/* An informational finding is an observation, not work. Traversal skips
          them, the rail badge ignores them and the seal does not wait on them,
          so offering a disposition here would be the one place that disagrees. */}
      {f.severity === "info" ? null : disposed ? (
        <div className="st-find__record">
          <p className="st-find__recorded">
            <CheckIcon />
            <span>
              <b>{DISPOSITION_LABEL[record!.disposition]}.</b> Recorded in this session.
            </span>
          </p>
          {record!.justification && <p className="st-find__just">{record!.justification}</p>}
          {(f.records?.length ?? 0) > 1 && (
            <p className="st-find__earlier">
              Earlier: {DISPOSITION_LABEL[f.records![f.records!.length - 2].disposition]}, recorded in this
              session.
            </p>
          )}
          <button
            type="button"
            className="st-btn st-btn--outline st-find__change"
            onClick={() => onPickDisposition(f, "disputed")}
          >
            Change
            <span className="studio__sr"> the disposition on {f.title}</span>
          </button>
        </div>
      ) : (
        <>
          {f.suggestion !== undefined && (
            <div className="st-fix">
              <p className="st-eyebrow st-fix__label">Suggested replacement</p>
              <p className="st-fix__text">{f.suggestion}</p>
              {applicable.ok ? (
                <button
                  type="button"
                  className="st-btn st-btn--primary st-fix__apply"
                  onClick={() => onApplySuggestion(f.id)}
                >
                  Apply suggested fix
                  <span className="studio__sr"> for {f.title}</span>
                </button>
              ) : (
                <button
                  type="button"
                  className="st-btn st-btn--primary st-fix__apply"
                  onClick={() => onRevert(f.blockId)}
                >
                  <UndoIcon size={12} />
                  Restore the checked text
                  <span className="studio__sr"> under {f.title}</span>
                </button>
              )}
            </div>
          )}

          <div className="st-disp" role="group" aria-label={`Disposition for ${f.title}`}>
            {DISPOSITIONS.map((d) => {
              // aria-disabled, never the disabled attribute: a disabled control is
              // skipped by assistive tech, so the one person who most needs the
              // gate explained would never reach the explanation.
              const blocked = d === "fixed" && !fixCheck.ok;
              // Fixed is the outcome the reviewer is working towards, so it is the
              // only filled control on the card and the other three argue quietly.
              const weight = d === "fixed" ? "st-btn--primary" : "st-btn--quiet";
              return (
                <button
                  key={d}
                  type="button"
                  className={`st-btn ${weight} st-disp__btn${editing === d ? " is-on" : ""}${
                    blocked ? " is-blocked" : ""
                  }`}
                  aria-disabled={blocked || undefined}
                  aria-describedby={blocked ? gateId : undefined}
                  onClick={() => onPickDisposition(f, d)}
                >
                  {DISPOSITION_LABEL[d]}
                </button>
              );
            })}
          </div>

          {/* Always in the DOM so aria-describedby resolves and the reason is
              spoken whenever Fixed is reached, but only drawn on the card the
              analyst is working. Repeating the same paragraph under every card
              turns the panel into a wall nobody reads. */}
          {!fixCheck.ok && (
            <p className={`st-disp__gate${active ? "" : " studio__sr"}`} id={gateId}>
              {gate}
            </p>
          )}

          {editing && editing !== "fixed" && (
            <div className="st-just">
              <label className="st-eyebrow st-just__label" htmlFor={`${uid}-field`}>
                {JUSTIFICATION_PROMPT[editing].label}
              </label>
              <p className="st-just__help" id={`${uid}-help`}>
                {JUSTIFICATION_PROMPT[editing].help}
              </p>
              <div className="st-field st-just__field">
                <textarea
                  id={`${uid}-field`}
                  ref={editorRef}
                  value={draft}
                  rows={3}
                  required
                  aria-required="true"
                  aria-invalid={cardError ? "true" : undefined}
                  aria-describedby={cardError ? `${uid}-help ${errorId}` : `${uid}-help`}
                  onChange={(e) => onDraftChange(f.id, e.target.value)}
                />
              </div>
              {cardError && (
                <p className="st-just__error" id={errorId}>
                  {errorMessage(cardError, editing, draft.trim().length)}
                </p>
              )}
              <div className="st-just__actions">
                <button type="button" className="st-btn st-btn--primary st-just__record" onClick={() => onRecord(f)}>
                  Record
                </button>
                <button type="button" className="st-btn st-btn--quiet st-just__cancel" onClick={onCancelPending}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          {cardError && !editing && (
            <p className="st-just__error" id={errorId} role="status">
              {errorMessage(cardError, "fixed", 0)}
            </p>
          )}
        </>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export function FindingsPanel(props: FindingsPanelProps) {
  const { doc, activeFindingId, copyFallback, onClose, onCopyRecord, onStep } = props;
  const v = verdictFor(doc);
  const findings = sortFindings(doc);
  const against = doc.standards.join(", ");
  const hasRecords = doc.findings.some((f) => currentRecord(f)) || (doc.closed?.length ?? 0) > 0;

  return (
    <>
      <div className="st-panel__head">
        <span className="st-panel__title">
          <ShieldIcon />
          Compliance results
        </span>
        <button
          type="button"
          className="st-icon-btn st-panel__close"
          onClick={onClose}
          aria-label="Close compliance results"
        >
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
                {v.blocking > 0 && (
                  <span className="st-chip st-count st-count--blocking">{v.blocking} blocking</span>
                )}
                <span className="st-chip st-count">{v.toResolve} to resolve</span>
                {v.notes > 0 && <span className="st-chip st-count">{countLabel(v.notes, "note")}</span>}
                {v.disposed > 0 && <span className="st-chip st-count st-count--done">{v.disposed} recorded</span>}
                {v.stale > 0 && <span className="st-chip st-count">{v.stale} stale</span>}
              </div>
              <p className="st-verdict__against">Checked against {against}.</p>
            </div>

            {findings.length > 1 && (
              <div className="st-step" role="group" aria-label="Move between open findings">
                <button
                  type="button"
                  className="st-btn st-btn--outline st-step__btn"
                  aria-keyshortcuts="Shift+F8"
                  onClick={() => onStep(-1)}
                >
                  <ArrowIcon className="st-step__up" />
                  Previous open finding
                </button>
                <button
                  type="button"
                  className="st-btn st-btn--outline st-step__btn"
                  aria-keyshortcuts="F8"
                  onClick={() => onStep(1)}
                >
                  <ArrowIcon />
                  Next open finding
                </button>
              </div>
            )}

            {v.tone === "clear" && doc.findings.length === 0 && (
              <p className="st-panel__empty">Nothing to resolve. Re-run the check after any edit.</p>
            )}

            {findings.length > 0 && (
              <ul className="st-find__list">
                {findings.map((f) => (
                  <FindingCard key={f.id} f={f} active={f.id === activeFindingId} {...props} />
                ))}
              </ul>
            )}
          </>
        )}
      </div>

      {hasRecords && (
        <div className="st-panel__foot">
          <button type="button" className="st-btn st-btn--outline st-record__copy" onClick={onCopyRecord}>
            <CopyIcon />
            Copy record
          </button>
          {copyFallback && (
            <>
              <label className="st-record__fallback-label" htmlFor="st-record-fallback">
                Copy this text
              </label>
              <textarea
                id="st-record-fallback"
                className="st-record__fallback"
                readOnly
                rows={4}
                value={copyFallback}
              />
            </>
          )}
          <p className="st-record__note">
            Working record. Not a controlled record and not an electronic signature. Copy these dispositions
            into the comment-resolution log to make them count.
          </p>
        </div>
      )}
    </>
  );
}
