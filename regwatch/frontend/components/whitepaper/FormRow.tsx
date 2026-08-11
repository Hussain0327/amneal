"use client";

import { useState } from "react";

import type { WhitepaperCell, WhitepaperCellMode, WhitepaperInput } from "@/lib/api";
import { relTime } from "@/lib/time";

// Why a cell can never be machine-filled, said once, in the form's own voice.
const MODE_LABEL: Record<WhitepaperCellMode, string> = {
  auto: "auto",
  evidence_only: "cited",
  manual: "analyst",
};

// Text equivalent for the status mark, so populated / absent / blank is never
// carried by color alone (WCAG 1.1.1 / 1.4.1).
const STATUS_LABEL: Record<WhitepaperCell["status"], string> = {
  populated: "Populated",
  verified_absent: "Verified absent",
  analyst_input_required: "Analyst input required",
};

export interface RowWorkflow {
  input: WhitepaperInput | null;
  /** A finalized run freezes the analyst layer: it renders as a record, never an editor. */
  frozen: boolean;
  onSave: (cellId: string, value: string) => Promise<void>;
  onClear: (cellId: string) => Promise<void>;
}

export function FormRow({
  cell,
  label,
  refs,
  workflow,
  reveal,
  onRegister,
}: {
  cell: WhitepaperCell;
  label: string;
  /** Footnote numbers into the provenance appendix. */
  refs: number[];
  /** null on the unpersisted inline result: there is no overlay layer to write to. */
  workflow: RowWorkflow | null;
  /** Stagger index for the fill-in reveal; -1 to render settled. */
  reveal: number;
  onRegister?: (cellId: string, el: HTMLElement | null) => void;
}) {
  const blank = cell.status === "analyst_input_required";
  const input = workflow?.input ?? null;
  const style =
    reveal >= 0 ? ({ animationDelay: `${Math.min(reveal * 16, 760)}ms` } as const) : undefined;

  return (
    <div
      className={`wp-row wp-row--${cell.status}`}
      ref={(el) => onRegister?.(cell.id, el)}
      data-cell={cell.id}
    >
      <div className="wp-row__label">
        <span>{label}</span>
        {blank && <span className={`wp-mode wp-mode--${cell.mode}`}>{MODE_LABEL[cell.mode]}</span>}
      </div>

      <div className="wp-row__value">
        {blank ? (
          <BlankField cell={cell} workflow={workflow} input={input} />
        ) : (
          <div className={reveal >= 0 ? "wp-val wp-ink" : "wp-val"} style={style}>
            {cell.status === "verified_absent" ? (
              <>
                <strong>No</strong>
                <span className="wp-absent">verified absent</span>
              </>
            ) : (
              // Empty-after-trim reads the same as null: a rule, never a blank
              // line passing for a populated cell.
              <span>{cell.value?.trim() ? cell.value : "\u2014"}</span>
            )}
            {refs.length > 0 && (
              <sup className="wp-refs">
                {refs.map((n) => (
                  <a key={n} href={`#wp-ref-${n}`} className="wp-refs__n">
                    [{n}]
                  </a>
                ))}
              </sup>
            )}
          </div>
        )}

        {cell.note && !blank && <p className="wp-note">{cell.note}</p>}

        {/* The analyst overlay is a separate layer from the cited value: on a
            blank it IS the answer, and on a populated cell it annotates one --
            the generated value above renders untouched either way (INV-3). */}
        {workflow && !blank && <NoteLayer cell={cell} input={input} workflow={workflow} />}
      </div>

      <span className="wp-row__flag" aria-hidden>
        {blank && !input ? "\u2691" : ""}
      </span>
      <span className="sr-only">{STATUS_LABEL[cell.status]}</span>
    </div>
  );
}

// An unfilled cell is a shaded blank on the form, the way an unfilled Word form
// field is -- and the analyst types straight onto it.
function BlankField({
  cell,
  workflow,
  input,
}: {
  cell: WhitepaperCell;
  workflow: RowWorkflow | null;
  input: WhitepaperInput | null;
}) {
  if (!workflow) {
    return (
      <div className="wp-blank wp-blank--inert">
        <span className="wp-blank__rule" aria-hidden />
        <span className="wp-blank__why">{cell.note || "Analyst input required"}</span>
      </div>
    );
  }
  if (workflow.frozen) {
    return input ? (
      <AnalystValue label="Analyst input" input={input} filled />
    ) : (
      <div className="wp-blank wp-blank--inert">
        <span className="wp-blank__rule" aria-hidden />
        <span className="wp-blank__why">Left blank at finalize</span>
      </div>
    );
  }
  return (
    <AnalystEditor
      cellId={cell.id}
      label="Analyst input"
      input={input}
      note={cell.note}
      alwaysOpen
      onSave={workflow.onSave}
      onClear={workflow.onClear}
    />
  );
}

function NoteLayer({
  cell,
  input,
  workflow,
}: {
  cell: WhitepaperCell;
  input: WhitepaperInput | null;
  workflow: RowWorkflow;
}) {
  if (workflow.frozen) {
    return input ? <AnalystValue label="Analyst note" input={input} /> : null;
  }
  return (
    <>
      {input && <AnalystValue label="Analyst note" input={input} />}
      <AnalystEditor
        cellId={cell.id}
        label="Analyst note"
        input={input}
        note={null}
        alwaysOpen={false}
        onSave={workflow.onSave}
        onClear={workflow.onClear}
      />
    </>
  );
}

function AnalystValue({
  label,
  input,
  filled,
}: {
  label: string;
  input: WhitepaperInput;
  filled?: boolean;
}) {
  return (
    <div className={filled ? "wp-analyst wp-analyst--filled" : "wp-analyst"}>
      <span className="wp-analyst__tag">{label}</span>
      <p>{input.value}</p>
      <span className="wp-analyst__meta">
        by {input.author ?? "unknown"}
        {relTime(input.updated_at) ? ` - ${relTime(input.updated_at)}` : ""}
      </span>
    </div>
  );
}

function AnalystEditor({
  cellId,
  label,
  input,
  note,
  alwaysOpen,
  onSave,
  onClear,
}: {
  cellId: string;
  label: string;
  input: WhitepaperInput | null;
  /** The populator's reason this cell was left to a human. */
  note: string | null;
  alwaysOpen: boolean;
  onSave: (cellId: string, value: string) => Promise<void>;
  onClear: (cellId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(input?.value ?? "");
  const [busy, setBusy] = useState(false);
  const [editorError, setEditorError] = useState<string | null>(null);
  const dirty = text !== (input?.value ?? "");

  async function save() {
    if (busy) return;
    setBusy(true);
    setEditorError(null);
    try {
      await onSave(cellId, text);
      setOpen(false);
    } catch (er) {
      setEditorError(er instanceof Error ? er.message : String(er));
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    if (busy) return;
    // Nothing stored: clearing is a local reset, not a server call.
    if (!input) {
      setText("");
      setOpen(false);
      return;
    }
    setBusy(true);
    setEditorError(null);
    try {
      await onClear(cellId);
      setText("");
      setOpen(false);
    } catch (er) {
      setEditorError(er instanceof Error ? er.message : String(er));
    } finally {
      setBusy(false);
    }
  }

  // A note on an already-cited cell stays folded away: 20 open editors would
  // bury the document they annotate.
  if (!alwaysOpen && !open) {
    return (
      <button
        className="wp-notebtn"
        type="button"
        aria-label={input ? "Edit note" : "Add note"}
        onClick={() => {
          // Re-seed from the stored value so an edit starts from what is saved,
          // not from stale local text.
          setText(input?.value ?? "");
          setEditorError(null);
          setOpen(true);
        }}
      >
        {input ? "Edit note" : "+ note"}
      </button>
    );
  }

  return (
    <div className={alwaysOpen ? "wp-fill" : "wp-fill wp-fill--note"}>
      <textarea
        className="wp-fill__input"
        aria-label={`${label} for ${cellId}`}
        rows={alwaysOpen ? 2 : 3}
        value={text}
        onChange={(e) => setText(e.target.value)}
        disabled={busy}
        placeholder={alwaysOpen ? (note ?? "Analyst input required") : "Add an attributed note..."}
      />
      {(dirty || !alwaysOpen || editorError) && (
        <div className="wp-fill__actions">
          <button className="wp-btn wp-btn--ink" type="button" onClick={() => void save()} disabled={busy || !text.trim()}>
            {busy ? "Saving..." : "Save"}
          </button>
          <button
            className="wp-btn"
            type="button"
            onClick={() => void clear()}
            disabled={busy || (!input && !text)}
          >
            Clear
          </button>
          {!alwaysOpen && (
            <button className="wp-btn" type="button" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </button>
          )}
        </div>
      )}
      {editorError && <p className="wp-fill__error">Save failed: {editorError}</p>}
      {alwaysOpen && input && (
        <span className="wp-analyst__meta">
          Saved by {input.author ?? "unknown"}
          {relTime(input.updated_at) ? ` - ${relTime(input.updated_at)}` : ""}
        </span>
      )}
    </div>
  );
}
