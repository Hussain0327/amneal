"use client";

import { useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import {
  ApiError,
  buildWhitepaper,
  downloadWhitepaperDocx,
  type WhitepaperCell,
  type WhitepaperCellMode,
  type WhitepaperCellStatus,
  type WhitepaperEvidence,
  type WhitepaperResponse,
  type WhitepaperSpine,
} from "@/lib/api";

const MODE_LABEL: Record<WhitepaperCellMode, string> = {
  auto: "auto",
  evidence_only: "evidence",
  manual: "manual",
};

// Timestamps may arrive without an offset (naive UTC from SQLite) — treat a
// missing offset as UTC, same convention as the sidebar history times.
function fmtWhen(iso: string): string {
  const norm = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
  const t = Date.parse(norm);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function tally(result: WhitepaperResponse) {
  let populated = 0;
  let absent = 0;
  let pending = 0;
  for (const section of result.sections) {
    for (const cell of section.cells) {
      if (cell.status === "populated") populated += 1;
      else if (cell.status === "verified_absent") absent += 1;
      else pending += 1;
    }
  }
  return { populated, absent, pending, total: populated + absent + pending };
}

export default function WhitepaperPage() {
  const [rld, setRld] = useState("");
  const [applNo, setApplNo] = useState("");
  const [result, setResult] = useState<WhitepaperResponse | null>(null);
  // 422 (spine could not resolve) is an expected, explanatory outcome and is
  // rendered inline as its own state — distinct from transport/server errors.
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const r = rld.trim();
    const a = applNo.trim();
    if (!r || !a || loading) return;
    setLoading(true);
    setError(null);
    setResolveError(null);
    setDownloadError(null);
    try {
      setResult(await buildWhitepaper(r, a));
    } catch (er) {
      setResult(null);
      if (er instanceof ApiError && er.status === 422) {
        setResolveError(er.detail || "The reference product and application number could not be resolved.");
      } else {
        setError(er instanceof Error ? er.message : String(er));
      }
    } finally {
      setLoading(false);
    }
  }

  // The .docx is rendered server-side FROM this exact result object (no
  // re-populate), so the download always matches the rendered paper even if
  // the form has been edited since.
  async function onDownload() {
    if (!result || downloading) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      await downloadWhitepaperDocx(result);
    } catch (er) {
      setDownloadError(er instanceof Error ? er.message : String(er));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="measure">
      <PageHeader
        index="04"
        product="White Paper"
        title="Populate the white paper."
        tagline="Every cell of the CRA template, traced to a public FDA record — filled where a source verifies it, handed to the analyst where judgment is required. It cites what it found; it never decides."
      />

      <form onSubmit={onSubmit} className="doc doc--pad rise d3">
        <div className="kicker" style={{ color: "var(--gold-ink)" }}>
          Intake
        </div>
        <div className="mt-4 grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(13rem, 1fr))" }}>
          <Field
            label="Reference product name"
            value={rld}
            onChange={setRld}
            placeholder="albuterol sulfate"
          />
          <Field
            label="Application number"
            value={applNo}
            onChange={setApplNo}
            placeholder="NDA 020503 · 020503 · N020503"
          />
        </div>
        <div className="mt-5">
          <button className="btn" type="submit" disabled={loading}>
            {loading ? "Populating…" : "Populate white paper"}
          </button>
        </div>
      </form>

      {loading && (
        <p className="code mt-7" style={{ fontSize: "0.74rem", color: "var(--ink-faint)" }}>
          Resolving the application and querying sources…
        </p>
      )}

      {resolveError && !loading && (
        <div className="stamp doc--seal mt-8 rise">
          <div className="stamp__tag">Could not resolve</div>
          <p style={{ margin: "0.5rem 0 0", fontSize: "0.98rem", lineHeight: 1.55, whiteSpace: "pre-wrap" }}>
            {resolveError}
          </p>
          <p className="code mt-3" style={{ fontSize: "0.72rem", margin: "0.8rem 0 0" }}>
            Nothing was guessed — check the name/number pair and resubmit.
          </p>
        </div>
      )}

      {error && !loading && (
        <div className="stamp mt-8 rise">
          <div className="stamp__tag">Request failed</div>
          <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
            {error}
          </p>
        </div>
      )}

      {result && !loading && (
        <section className="mt-9 rise">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
            <h2 className="kicker" style={{ color: "var(--ink)" }}>
              White paper
            </h2>
            <hr className="hair grow" />
            <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
              audit #{result.audit_id}
            </span>
            {/* Only rendered once a result exists — the docx call sends that
                result verbatim, so there is never a button without one. */}
            <button className="btn" onClick={() => void onDownload()} disabled={downloading}>
              {downloading ? "Preparing…" : "Download .docx"}
            </button>
          </div>
          {downloadError && (
            <p className="code mt-2" style={{ fontSize: "0.78rem", color: "var(--oxblood)" }}>
              Download failed: {downloadError}
            </p>
          )}

          <div className="mt-4">
            <SpineCard spine={result.spine} extraWarnings={result.warnings} />
          </div>

          <Tally result={result} />

          {result.sections.map((section, i) => (
            <section key={section.title} className="mt-8">
              <div className="flex items-baseline gap-3">
                <span className="kicker" style={{ color: "var(--ink-faint)" }}>
                  {String(i + 1).padStart(2, "0")}
                </span>
                <h3 className="kicker" style={{ color: "var(--ink)" }}>
                  {section.title}
                </h3>
                <hr className="hair grow" />
                <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
                  {section.cells.length} cells
                </span>
              </div>
              <div className="doc doc--pad mt-3">
                {section.cells.map((cell) => (
                  <Cell key={cell.id} cell={cell} />
                ))}
              </div>
            </section>
          ))}
        </section>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div>
      <label className="kicker" style={{ color: "var(--ink-faint)" }}>
        {label}
      </label>
      <input className="field mt-1" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
    </div>
  );
}

function SpineCard({ spine, extraWarnings }: { spine: WhitepaperSpine; extraWarnings: string[] }) {
  const warnings = Array.from(new Set([...spine.warnings, ...extraWarnings]));
  return (
    <div className="doc doc--seal doc--pad">
      <div className="kicker" style={{ color: "var(--gold-ink)" }}>
        Resolution spine
      </div>
      <div className="mt-4 grid gap-x-6 gap-y-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(11rem, 1fr))" }}>
        <SpineItem label="Application">
          <span className="code">
            {spine.application_type} {spine.application_number}
          </span>
        </SpineItem>
        <SpineItem label="Ingredient">{spine.ingredient}</SpineItem>
        <SpineItem label="Normalized name">
          <span className="code">{spine.normalized_name}</span>
        </SpineItem>
        <SpineItem label="Products">
          {spine.product_numbers.length === 0 ? (
            "—"
          ) : (
            <span className="flex flex-wrap gap-1.5">
              {spine.product_numbers.map((p) => (
                <span key={p} className="chip code">
                  {p}
                </span>
              ))}
            </span>
          )}
        </SpineItem>
        <SpineItem label="DailyMed SPL">
          {spine.setid ? <span className="code" style={{ wordBreak: "break-all" }}>{spine.setid}</span> : "—"}
        </SpineItem>
      </div>
      {warnings.length > 0 && (
        <div className="wp-warn">
          <span className="kicker" style={{ fontSize: "0.6rem" }}>
            Warnings
          </span>
          <ul>
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function SpineItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="kicker" style={{ fontSize: "0.6rem", color: "var(--ink-faint)" }}>
        {label}
      </div>
      <div style={{ marginTop: "0.3rem", fontSize: "0.92rem", color: "var(--ink)" }}>{children}</div>
    </div>
  );
}

// Counts double as the legend: each line carries the same status glyph the
// cells use, so the three states read the same everywhere.
function Tally({ result }: { result: WhitepaperResponse }) {
  const t = tally(result);
  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-1.5">
      <TallyItem status="populated" text={`${t.populated} populated`} />
      <TallyItem status="verified_absent" text={`${t.absent} verified absent — rendered “No”`} />
      <TallyItem status="analyst_input_required" text={`${t.pending} analyst input required`} />
      <span className="code" style={{ fontSize: "0.68rem", color: "var(--ink-faint)", marginLeft: "auto" }}>
        {t.total} cells
      </span>
    </div>
  );
}

function TallyItem({ status, text }: { status: WhitepaperCellStatus; text: string }) {
  return (
    <span className="flex items-center gap-2">
      <span className={`wp-dot wp-dot--${status}`} aria-hidden />
      <span className="code" style={{ fontSize: "0.7rem", color: "var(--ink-soft)" }}>
        {text}
      </span>
    </span>
  );
}

function Cell({ cell }: { cell: WhitepaperCell }) {
  return (
    <div className="wp-cell">
      <div className="wp-cell__head">
        <span className={`wp-dot wp-dot--${cell.status}`} aria-hidden />
        <span className="wp-cell__label">{cell.label}</span>
        <span className={`wp-badge wp-badge--${cell.mode}`}>{MODE_LABEL[cell.mode]}</span>
      </div>

      {cell.status === "analyst_input_required" ? (
        <div className="wp-pending">
          <span className="wp-pending__tag">Analyst input required</span>
          {cell.note && <p>{cell.note}</p>}
        </div>
      ) : cell.status === "verified_absent" ? (
        // The compliant "No": the source was queried and the record is
        // genuinely absent; the query itself is recorded in evidence.
        <p className="wp-cell__value">
          <strong>No</strong>
          <span className="wp-absent">verified absent</span>
        </p>
      ) : (
        // An empty/whitespace value reads the same as null: an em-dash, never
        // a blank line passing for a populated cell.
        <p className="wp-cell__value">{cell.value?.trim() ? cell.value : "—"}</p>
      )}

      {cell.note && cell.status !== "analyst_input_required" && <p className="wp-cell__note">{cell.note}</p>}

      {cell.evidence.length > 0 && (
        <details className="wp-cell__evidence">
          <summary className="kicker" style={{ cursor: "pointer", fontSize: "0.6rem", color: "var(--ink-faint)" }}>
            Evidence · {cell.evidence.length}
          </summary>
          <div className="mt-1">
            {cell.evidence.map((ev, i) => (
              <EvidenceRow key={`${ev.source}-${ev.locator}-${i}`} n={i + 1} ev={ev} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function EvidenceRow({ n, ev }: { n: number; ev: WhitepaperEvidence }) {
  const where = [ev.page !== null ? `p.${ev.page}` : null, ev.section].filter(Boolean).join(" · ");
  return (
    <div className="ref">
      <span className="ref__no">[{n}]</span>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="ref__src">{ev.source}</span>
        <span className="code" style={{ fontSize: "0.74rem", color: "var(--ink-soft)", wordBreak: "break-all" }}>
          {ev.locator}
        </span>
        {where && <span className="ref__page">· {where}</span>}
        {ev.fetched_at && (
          <span className="code" style={{ fontSize: "0.68rem", color: "var(--ink-faint)" }}>
            fetched {fmtWhen(ev.fetched_at)}
          </span>
        )}
      </div>
      {ev.snippet && <blockquote className="ref__quote">{ev.snippet}</blockquote>}
      {ev.source_url && (
        <a className="link code" style={{ fontSize: "0.76rem" }} href={ev.source_url} target="_blank" rel="noreferrer">
          {ev.source_url}
        </a>
      )}
    </div>
  );
}
