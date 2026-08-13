"use client";

import { useMemo } from "react";

import { FormRow } from "@/components/whitepaper/FormRow";
import { type FlowItem, PaperDoc } from "@/components/whitepaper/PaperDoc";
import type { WhitepaperInput, WhitepaperSectionData, WhitepaperSpine } from "@/lib/api";
import { formatWhen } from "@/lib/time";
import { safeHref } from "@/lib/url";
import { buildRefs, type FormRef, groupCells } from "@/lib/whitepaper-form";

const APPENDIX_TITLE = "Appendix A \u2014 Provenance";

export const RUNNING_HEAD = "Clinical Regulatory Affairs / Labeling White Paper";

export interface DocMeta {
  spine: WhitepaperSpine;
  warnings: string[];
  auditId: number;
  runId: number | null;
  status: "draft" | "final" | "unsaved";
  preparedBy: string | null;
  preparedAt: string | null;
  finalizedAt: string | null;
  finalizedBy: string | null;
}

export interface DocWorkflow {
  frozen: boolean;
  inputs: Record<string, WhitepaperInput>;
  onSave: (cellId: string, value: string) => Promise<void>;
  onClear: (cellId: string) => Promise<void>;
}

/**
 * The white paper as the document it is: the CRA form's seven tables on
 * landscape sheets, a provenance appendix behind them, and the analyst's blanks
 * waiting on the page where they will print.
 */
export function WhitePaperDocument({
  meta,
  sections,
  workflow,
  scale,
  paged,
  reveal,
  onLayout,
  onRegisterRow,
}: {
  meta: DocMeta;
  sections: WhitepaperSectionData[];
  /** null on an unpersisted result: there is no overlay layer to write to. */
  workflow: DocWorkflow | null;
  scale: number;
  paged: boolean;
  /** Ink the values in on arrival, once, right after a populate. */
  reveal: boolean;
  onLayout?: (pages: number) => void;
  onRegisterRow?: (cellId: string, el: HTMLElement | null) => void;
}) {
  const groups = useMemo(() => groupCells(sections), [sections]);
  const { refs, byCell } = useMemo(() => buildRefs(groups), [groups]);

  const items = useMemo(() => {
    const out: FlowItem[] = [];
    out.push({ key: "masthead", node: <Masthead meta={meta} /> });
    if (meta.warnings.length > 0) {
      out.push({ key: "notice", node: <Notice warnings={meta.warnings} /> });
    }
    let n = 0;
    groups.forEach((group, gi) => {
      out.push({
        key: `band-${gi}`,
        keepWithNext: true,
        section: group.title,
        node: <Band title={group.title} count={group.rows.length} />,
      });
      for (const row of group.rows) {
        const index = n;
        n += 1;
        out.push({
          key: `row-${row.cell.id}`,
          section: group.title,
          node: (
            <FormRow
              cell={row.cell}
              label={row.label}
              refs={byCell.get(row.cell.id) ?? []}
              workflow={
                workflow
                  ? {
                      input: workflow.inputs[row.cell.id] ?? null,
                      frozen: workflow.frozen,
                      onSave: workflow.onSave,
                      onClear: workflow.onClear,
                    }
                  : null
              }
              reveal={reveal ? index : -1}
              onRegister={onRegisterRow}
            />
          ),
        });
      }
    });
    if (refs.length > 0) {
      out.push({
        key: "appendix",
        keepWithNext: true,
        section: APPENDIX_TITLE,
        node: <Band title={APPENDIX_TITLE} count={refs.length} appendix />,
      });
      for (const ref of refs) {
        out.push({ key: `ref-${ref.n}`, section: APPENDIX_TITLE, node: <RefRow entry={ref} /> });
      }
    }
    return out;
  }, [byCell, groups, meta, onRegisterRow, refs, reveal, workflow]);

  return (
    <PaperDoc
      items={items}
      runningHead={RUNNING_HEAD}
      scale={scale}
      paged={paged}
      onLayout={onLayout}
      stamp={<Stamp status={meta.status} />}
    />
  );
}

function Stamp({ status }: { status: DocMeta["status"] }) {
  if (status === "draft") return null;
  return (
    <span className={`wp-stamp wp-stamp--${status === "final" ? "final" : "unsaved"}`}>
      {status === "final" ? "final" : "not saved"}
    </span>
  );
}

function Masthead({ meta }: { meta: DocMeta }) {
  const { spine } = meta;
  return (
    <header className="wp-mast">
      <p className="wp-mast__product">
        {spine.ingredient || "\u2014"}
        <span className="wp-mast__appl">
          {spine.application_type} {spine.application_number}
        </span>
      </p>

      <dl className="wp-mast__grid">
        <Fact k="Normalized name" v={spine.normalized_name || "\u2014"} mono />
        <Fact
          k="Products"
          v={spine.product_numbers.length > 0 ? spine.product_numbers.join(", ") : "\u2014"}
          mono
        />
        <Fact
          k="Drugs@FDA label"
          v={spine.approved_label_document_id || "\u2014"}
          mono
          break
        />
        <Fact k="Prepared by" v={meta.preparedBy || "\u2014"} />
      </dl>

      <p className="wp-mast__docket">
        {meta.runId !== null ? `run #${meta.runId} / ` : ""}audit #{meta.auditId}
      </p>

      {/* Freshness is honest, not implied: the generated layer is immutable, so
          refreshing the data means a NEW run and this one stays as filed. */}
      {meta.preparedAt && (
        <p className="wp-mast__asof">
          Data as of {formatWhen(meta.preparedAt)} - re-populate to refresh.
          {meta.preparedBy ? ` Created by ${meta.preparedBy}.` : ""}
          {meta.status === "final" && meta.finalizedAt && (
            <span>
              {" "}
              Finalized {formatWhen(meta.finalizedAt)}
              {meta.finalizedBy ? ` by ${meta.finalizedBy}` : ""}.
            </span>
          )}
        </p>
      )}
    </header>
  );
}

function Fact({ k, v, mono, break: brk }: { k: string; v: string; mono?: boolean; break?: boolean }) {
  return (
    <div className="wp-fact">
      <dt>{k}</dt>
      <dd className={`${mono ? "wp-fact__mono" : ""}${brk ? " wp-fact__break" : ""}`}>{v}</dd>
    </div>
  );
}

function Notice({ warnings }: { warnings: string[] }) {
  const unique = Array.from(new Set(warnings));
  return (
    <aside className="wp-notice">
      <span className="wp-notice__tag">Notice</span>
      <ul>
        {unique.map((w) => (
          <li key={w}>{w}</li>
        ))}
      </ul>
    </aside>
  );
}

function Band({
  title,
  count,
  appendix,
}: {
  title: string;
  count: number;
  appendix?: boolean;
}) {
  return (
    <h2 className={appendix ? "wp-band wp-band--appendix" : "wp-band"}>
      <span>{title}</span>
      <span className="wp-band__count">{count}</span>
    </h2>
  );
}

function RefRow({ entry }: { entry: FormRef }) {
  const { ev } = entry;
  const where = [ev.page !== null ? `p.${ev.page}` : null, ev.section].filter(Boolean).join(" \u00b7 ");
  return (
    <div className="wp-ref" id={`wp-ref-${entry.n}`}>
      <span className="wp-ref__n">[{entry.n}]</span>
      <div className="wp-ref__body">
        <p className="wp-ref__head">
          <span className="wp-ref__src">{ev.source}</span>
          <span className="wp-ref__loc">{ev.locator}</span>
          {where && <span className="wp-ref__where">{where}</span>}
          {ev.fetched_at && (
            <span className="wp-ref__when">fetched {formatWhen(ev.fetched_at)}</span>
          )}
        </p>
        {ev.snippet && <blockquote className="wp-ref__quote">{ev.snippet}</blockquote>}
        <p className="wp-ref__cited">Cited at: {entry.citedBy.join("; ")}</p>
        {ev.source_url && (
          <a
            className="wp-ref__link"
            href={safeHref(ev.source_url)}
            target="_blank"
            rel="noreferrer"
          >
            {ev.source_url}
          </a>
        )}
      </div>
    </div>
  );
}
