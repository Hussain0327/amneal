"use client";

import { StatusChip } from "@/components/whitepaper/DocChrome";
import type { WhitepaperRunSummary } from "@/lib/api";
import { relTime } from "@/lib/time";

const LINES = 13;

/**
 * A saved run drawn as the sheet it is: ruled lines standing in for the 46
 * cells, inked where a source answered, hatched where the record is verifiably
 * absent, gold where a blank is still waiting. Reading a shelf of these is
 * faster than reading three counts off each row.
 */
function Thumb({ run }: { run: WhitepaperRunSummary }) {
  const total = Math.max(
    1,
    run.populated_count + run.verified_absent_count + run.analyst_input_count,
  );
  const filledBlanks = Math.min(run.inputs_count, run.analyst_input_count);
  const buckets: string[] = [];
  for (let i = 0; i < LINES; i += 1) {
    const at = ((i + 0.5) / LINES) * total;
    if (at < run.populated_count) buckets.push("pop");
    else if (at < run.populated_count + run.verified_absent_count) buckets.push("abs");
    else if (at < run.populated_count + run.verified_absent_count + filledBlanks) buckets.push("fill");
    else buckets.push("gap");
  }
  return (
    <svg className="wp-thumb" viewBox="0 0 110 85" aria-hidden focusable="false">
      <rect className="wp-thumb__paper" x="0.5" y="0.5" width="109" height="84" rx="1.5" />
      <rect className="wp-thumb__band" x="9" y="9" width="92" height="6" />
      {buckets.map((kind, i) => (
        <rect
          key={i}
          className={`wp-thumb__line wp-thumb__line--${kind}`}
          x="9"
          y={20 + i * 4.6}
          width={kind === "gap" ? 44 : 92}
          height="2.4"
        />
      ))}
    </svg>
  );
}

export function RunCard({
  run,
  open,
  onOpen,
  deleteConfirm,
  deleteBusy,
  deleteError,
  onDeleteAsk,
  onDeleteCancel,
  onDeleteConfirm,
}: {
  run: WhitepaperRunSummary;
  open: boolean;
  onOpen: () => void;
  deleteConfirm: boolean;
  deleteBusy: boolean;
  deleteError: string | null;
  onDeleteAsk: () => void;
  onDeleteCancel: () => void;
  onDeleteConfirm: () => void;
}) {
  return (
    <article className={open ? "wp-card wp-card--open" : "wp-card"} aria-current={open ? "true" : undefined}>
      {/* Decorative: the name below is the one open affordance, so the card
          never offers two controls that do the same thing. */}
      <div className="wp-card__sheet">
        <Thumb run={run} />
      </div>
      <div className="wp-card__body">
        <div className="wp-card__top">
          <button type="button" className="wp-card__name" onClick={onOpen}>
            {run.ingredient || run.rld_name_input}
          </button>
          <StatusChip status={run.status} />
        </div>
        <p className="wp-card__appl">
          {run.application_type} {run.application_number}
          <span className="wp-card__age">{relTime(run.updated_at)}</span>
        </p>
        <p className="wp-card__counts">
          <span>
            {run.populated_count} populated / {run.verified_absent_count} absent /{" "}
            {run.analyst_input_count} analyst
          </span>
          <span>
            {run.inputs_count} {run.inputs_count === 1 ? "input" : "inputs"}
          </span>
          <span>by {run.created_by}</span>
        </p>
        <div className="wp-card__actions">
          {!deleteConfirm && (
            <button className="wp-btn wp-btn--quiet" type="button" onClick={onDeleteAsk} disabled={deleteBusy}>
              delete
            </button>
          )}
          {deleteConfirm && (
            <>
              <span className="wp-card__ask">Delete this run?</span>
              <button className="wp-btn wp-btn--danger" type="button" onClick={onDeleteConfirm} disabled={deleteBusy}>
                {deleteBusy ? "deleting" : "confirm"}
              </button>
              <button className="wp-btn wp-btn--quiet" type="button" onClick={onDeleteCancel} disabled={deleteBusy}>
                cancel
              </button>
            </>
          )}
        </div>
        {deleteError && <p className="wp-card__error">Delete failed: {deleteError}</p>}
      </div>
    </article>
  );
}
