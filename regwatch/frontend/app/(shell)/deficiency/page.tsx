"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { ApiError, analyzeDeficiency, getDeficiencyRun, listDeficiencyRuns } from "@/lib/api";
import type {
  DeficiencyRunDetail,
  DeficiencyRunStatus,
  DeficiencyRunSummary,
  EvidenceClass,
  Fault,
  FaultReport,
  ParseFailed,
  Severity,
  SimilarDeficiency,
  Tier,
} from "@/lib/deficiency-types";

// The server rejects anything larger with a 400; guarding here too means an
// obviously-too-big file never spends the upload. Binary MB, matching the
// usual "50MB" reading of a file-size cap.
const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
const POLL_INTERVAL_MS = 2500;
// Ten minutes. Past this the page stops asking and says so -- the run is NOT
// cancelled server-side, it is just no longer watched from here.
const POLL_DEADLINE_MS = 600_000;

// Order is the contract: strongest evidence first, recall last.
const TIER_ORDER: Tier[] = ["verified", "corroborated", "advisory"];

const TIER_LABEL: Record<Tier, string> = {
  verified: "Verified",
  corroborated: "Corroborated",
  advisory: "Advisory",
};

// Says what standing the tier actually carries, so "advisory" is never read as
// a weaker version of the same claim.
const TIER_NOTE: Record<Tier, string> = {
  verified: "Oracle-confirmed, or strong precedent with self-consistency.",
  corroborated: "At least one real precedent; no hard oracle.",
  advisory: "Model judgment only, including novel or out-of-distribution findings.",
};

const EVIDENCE_LABEL: Record<EvidenceClass, string> = {
  code_verified: "code verified",
  checklist: "checklist",
  quote_anchored: "quote anchored",
  model_judgment: "model judgment",
};

const STATUS_NOTE: Record<DeficiencyRunStatus, string> = {
  pending: "Queued. The document has been accepted and is waiting for a worker.",
  running: "Reading the document and checking it against the deficiency knowledge base...",
  complete: "Analysis complete.",
  failed: "The analysis failed.",
};

// Timestamps may arrive without an offset (naive UTC from the store) -- treat a
// missing offset as UTC, the same convention the White Paper / Watch pages use.
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

// snake_case enum member -> readable label. Falls back to the raw value, so a
// category this build does not know still renders as itself.
function humanize(value: string): string {
  return value ? value.replace(/_/g, " ") : "";
}

function fmtMb(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(1);
}

// 0..1 similarity -> whole percent. A 0 / non-finite score renders nothing
// rather than an authoritative-looking "0% match".
function pctMatch(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return "";
  return `${Math.round(v * 100)}% match`;
}

// Client-side guard. Returns the reason to refuse, or null to allow.
function rejectReason(file: File): string | null {
  const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  if (!isPdf) {
    return "That is not a PDF. Upload the submission section as a PDF file.";
  }
  if (file.size === 0) {
    return "That file is empty.";
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return `That file is ${fmtMb(file.size)} MB; the limit is 50 MB.`;
  }
  return null;
}

function isTerminal(status: DeficiencyRunStatus | null): boolean {
  return status === "complete" || status === "failed";
}

export default function DeficiencyPage() {
  // --- upload ---
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // --- the run being watched (the one this page just started) ---
  const [activeRunId, setActiveRunId] = useState<number | null>(null);
  const [activeRun, setActiveRun] = useState<DeficiencyRunDetail | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [pollExpired, setPollExpired] = useState(false);

  // --- runs list ---
  const [runs, setRuns] = useState<DeficiencyRunSummary[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runsBusy, setRunsBusy] = useState(false);
  const runsLoadingRef = useRef(false);

  // --- the report on screen (a finished watch, or a row the analyst opened) ---
  const [selected, setSelected] = useState<DeficiencyRunDetail | null>(null);
  const [selectedLoading, setSelectedLoading] = useState<number | null>(null);
  const [selectedError, setSelectedError] = useState<string | null>(null);

  const loadRuns = useCallback(() => {
    // In-flight guard: collapse overlapping loads (mount, Refresh, a poll that
    // just ended) into one request -- the Watch / White Paper pattern.
    if (runsLoadingRef.current) return;
    runsLoadingRef.current = true;
    setRunsBusy(true);
    setRunsError(null);
    listDeficiencyRuns()
      .then((d) => setRuns(d.runs))
      // Leave `runs` untouched on failure -- an error must never masquerade as
      // a loaded-but-empty list.
      .catch((e) => setRunsError(e instanceof Error ? e.message : String(e)))
      .finally(() => {
        runsLoadingRef.current = false;
        setRunsBusy(false);
      });
  }, []);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  // Poll the watched run until it is complete or failed. Every state write
  // happens inside a timer/promise callback (never in the effect body), the
  // interval is cleared on EVERY exit path including unmount, and a late
  // response after unmount / after switching runs is dropped by `cancelled`.
  useEffect(() => {
    if (activeRunId === null) return;
    const runId = activeRunId;
    const startedAt = Date.now();
    let cancelled = false;
    // Serializes the polls: a slow response can never stack a second request
    // on top of itself.
    let inFlight = false;

    const timer = setInterval(() => {
      if (cancelled || inFlight) return;
      if (Date.now() - startedAt >= POLL_DEADLINE_MS) {
        clearInterval(timer);
        setPollExpired(true);
        loadRuns();
        return;
      }
      inFlight = true;
      getDeficiencyRun(runId)
        .then((detail) => {
          if (cancelled) return;
          setPollError(null);
          setActiveRun(detail);
          if (!isTerminal(detail.status)) return;
          clearInterval(timer);
          // A complete run carries its report; a failed one carries its error
          // and must never render as an empty report.
          if (detail.status === "complete") {
            setSelected(detail);
            setSelectedError(null);
          }
          loadRuns();
        })
        .catch((e) => {
          if (cancelled) return;
          const message = e instanceof Error ? e.message : String(e);
          setPollError(message);
          // A 404 can never become a 200: the run does not exist, so stop
          // asking instead of burning the full deadline. Anything else
          // (transport blip, 5xx) keeps polling -- the next tick may succeed.
          if (e instanceof ApiError && e.status === 404) {
            clearInterval(timer);
            setPollExpired(true);
            loadRuns();
          }
        })
        .finally(() => {
          inFlight = false;
        });
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [activeRunId, loadRuns]);

  // Status of the watched run: "pending" until the first poll lands (that is
  // exactly what the 202 said), then whatever the store reports.
  const activeStatus: DeficiencyRunStatus | null =
    activeRunId === null ? null : (activeRun?.status ?? "pending");
  // A run is in flight while it is watched, not terminal, and not abandoned.
  const watching = activeRunId !== null && !pollExpired && !isTerminal(activeStatus);
  const busy = submitting || watching;

  function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = e.target.files?.[0] ?? null;
    setSubmitError(null);
    if (!picked) {
      setFile(null);
      setFileError(null);
      return;
    }
    const reason = rejectReason(picked);
    setFileError(reason);
    // Refuse rather than half-accept: a rejected file is never submittable.
    setFile(reason === null ? picked : null);
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file || busy) return;
    // Re-check at submit time: the guard must hold even if the file changed
    // under a stale render.
    const reason = rejectReason(file);
    if (reason !== null) {
      setFileError(reason);
      setFile(null);
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    setPollError(null);
    setPollExpired(false);
    setActiveRun(null);
    // The previous report is not this run's -- clear it so the two can never
    // be read together.
    setSelected(null);
    setSelectedError(null);
    try {
      const accepted = await analyzeDeficiency(file);
      setActiveRunId(accepted.run_id);
    } catch (er) {
      // 400 (not a PDF / too large) and 429 arrive with the server's own
      // detail; surface it verbatim rather than guessing at the cause.
      setSubmitError(er instanceof Error ? er.message : String(er));
    } finally {
      setSubmitting(false);
    }
  }

  function openRun(id: number) {
    if (selectedLoading !== null) return;
    setSelectedLoading(id);
    setSelectedError(null);
    getDeficiencyRun(id)
      .then((detail) => {
        setSelected(detail);
      })
      .catch((e) => {
        setSelected(null);
        setSelectedError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setSelectedLoading(null));
  }

  return (
    <div className="measure">
      <PageHeader
        index="05"
        product="Deficiency"
        title="Read it the way FDA will."
        tagline="Upload a CMC section and it returns candidate deficiencies, each ranked by how far the evidence stands behind it: recomputed by an oracle, corroborated by a real precedent, or flagged as model judgment. It surfaces and cites; the analyst decides."
      />

      <form onSubmit={onSubmit} className="doc doc--pad rise d3">
        <div className="kicker" style={{ color: "var(--gold-ink)" }}>
          Upload
        </div>
        <p className="mt-3" style={{ color: "var(--ink-soft)", fontSize: "0.92rem", lineHeight: 1.55 }}>
          One PDF, up to 50 MB. Nothing is filed or submitted anywhere; the document is read to
          produce the fault report below.
        </p>
        <div className="mt-4">
          <label
            className="kicker"
            htmlFor="deficiency-file"
            style={{ fontSize: "0.6rem", color: "var(--ink-faint)", display: "block" }}
          >
            PDF file
          </label>
          <input
            id="deficiency-file"
            className="mt-2"
            type="file"
            accept="application/pdf"
            onChange={onPick}
            disabled={busy}
            style={{ fontSize: "0.88rem", color: "var(--ink-2)", display: "block" }}
          />
        </div>
        {file && !fileError && (
          <p className="code mt-2" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            {file.name} - {fmtMb(file.size)} MB
          </p>
        )}
        {fileError && (
          <p className="code mt-2" style={{ fontSize: "0.76rem", color: "var(--oxblood)" }}>
            {fileError}
          </p>
        )}
        <div className="mt-5">
          <button className="btn" type="submit" disabled={busy || !file}>
            {submitting ? "Uploading..." : watching ? "Analyzing..." : "Analyze"}
          </button>
        </div>
      </form>

      {submitError && (
        <div className="stamp mt-8 rise">
          <div className="stamp__tag">Upload failed</div>
          <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
            {submitError}
          </p>
        </div>
      )}

      {activeRunId !== null && (
        <section className="mt-8 rise" aria-busy={watching}>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h2 className="kicker" style={{ color: "var(--ink)" }}>
              This run
            </h2>
            <StatusPill status={activeStatus ?? "pending"} />
            <hr className="hair grow" />
            <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
              run #{activeRunId}
            </span>
          </div>
          <p className="mt-2" style={{ color: "var(--ink-soft)", fontSize: "0.92rem", lineHeight: 1.55 }}>
            {STATUS_NOTE[activeStatus ?? "pending"]}
          </p>
          {activeRun?.status === "failed" && activeRun.error && (
            <p className="code mt-2" style={{ fontSize: "0.78rem", color: "var(--oxblood)" }}>
              {activeRun.error}
            </p>
          )}
          {pollExpired && !isTerminal(activeStatus) && (
            <p className="code mt-2" style={{ fontSize: "0.78rem", color: "var(--oxblood)" }}>
              Still running - check the runs list later.
            </p>
          )}
          {pollError && !pollExpired && !isTerminal(activeStatus) && (
            <p className="code mt-2" style={{ fontSize: "0.74rem", color: "var(--ink-faint)" }}>
              Last status check failed ({pollError}); still trying.
            </p>
          )}
        </section>
      )}

      {selectedError && (
        <div className="stamp mt-8 rise">
          <div className="stamp__tag">Report unavailable</div>
          <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
            {selectedError}
          </p>
        </div>
      )}

      {selected && <ReportSection run={selected} />}

      <section className="mt-10 rise d4">
        <div className="flex items-baseline gap-3">
          <h2 className="kicker" style={{ color: "var(--ink)" }}>
            Runs
          </h2>
          <hr className="hair grow" />
          <button
            className="btn btn--ghost"
            type="button"
            onClick={loadRuns}
            disabled={runsBusy}
            style={{ padding: "0.32rem 0.7rem", fontSize: "0.62rem", opacity: runsBusy ? 0.6 : 1 }}
          >
            {runsBusy ? "Refreshing" : "Refresh"}
          </button>
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            {runsError ? "-" : runs ? `${runs.length} runs` : "..."}
          </span>
        </div>

        {runsError && (
          <div className="stamp mt-3">
            <div className="stamp__tag">Runs unavailable</div>
            <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
              {runsError}
            </p>
            {/* disabled while a load is in flight: loadRuns() no-ops behind the
                guard, so an enabled button would be a dead retry. */}
            <button className="btn btn--ghost mt-3" type="button" onClick={loadRuns} disabled={runsBusy}>
              Try again
            </button>
          </div>
        )}

        {!runsError && runs && runs.length === 0 && (
          <p className="mt-3" style={{ color: "var(--ink-soft)", fontSize: "0.95rem" }}>
            No runs yet. Upload a PDF above and the analysis will appear here.
          </p>
        )}

        {!runsError && runs && runs.length > 0 && (
          <div className="mt-3 flex flex-col gap-3">
            {runs.map((r) => (
              <RunRow
                key={r.id}
                run={r}
                open={selected !== null && selected.id === r.id}
                loading={selectedLoading === r.id}
                onOpen={() => openRun(r.id)}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function StatusPill({ status }: { status: DeficiencyRunStatus }) {
  // complete borrows the gold treatment, failed the oxblood one; pending and
  // running keep the neutral chip. The word itself is always present, so the
  // state is never conveyed by color alone.
  const style =
    status === "complete"
      ? { color: "var(--gold-ink)", background: "var(--gold-wash)", borderColor: "var(--gold-deep)" }
      : status === "failed"
        ? { color: "var(--oxblood)", background: "var(--oxblood-wash)", borderColor: "var(--oxblood)" }
        : undefined;
  return (
    <span className="chip code" style={style}>
      {status}
    </span>
  );
}

function RunRow({
  run,
  open,
  loading,
  onOpen,
}: {
  run: DeficiencyRunSummary;
  open: boolean;
  loading: boolean;
  onOpen: () => void;
}) {
  // Only a complete run has a report to open; every other status renders its
  // filename as plain text rather than a button that could only fail.
  const openable = run.status === "complete";
  return (
    <article className={`doc doc--pad${open ? " doc--seal" : ""}`} aria-current={open ? "true" : undefined}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {openable ? (
          <button
            type="button"
            className="link display"
            onClick={onOpen}
            disabled={loading}
            style={{
              fontSize: "1.1rem",
              fontWeight: 600,
              background: "none",
              border: 0,
              padding: 0,
              cursor: "pointer",
            }}
          >
            {run.filename}
          </button>
        ) : (
          <span className="display" style={{ fontSize: "1.1rem", fontWeight: 600 }}>
            {run.filename}
          </span>
        )}
        <StatusPill status={run.status} />
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)", marginLeft: "auto" }}>
          {fmtWhen(run.created_at)}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
          {run.fault_count === null
            ? "faults pending"
            : `${run.fault_count} ${run.fault_count === 1 ? "fault" : "faults"}`}
        </span>
        {run.page_count !== null && (
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
            {run.page_count} {run.page_count === 1 ? "page" : "pages"}
          </span>
        )}
        {run.completed_at && (
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            finished {fmtWhen(run.completed_at)}
          </span>
        )}
        {loading && (
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            loading report...
          </span>
        )}
      </div>
      {run.status === "failed" && (
        <p className="code mt-2" style={{ fontSize: "0.74rem", color: "var(--oxblood)" }}>
          {run.error ? run.error : "The analysis failed; no reason was recorded."}
        </p>
      )}
    </article>
  );
}

function ReportSection({ run }: { run: DeficiencyRunDetail }) {
  const report = run.report;
  return (
    <section className="mt-9 rise">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="kicker" style={{ color: "var(--ink)" }}>
          Fault report
        </h2>
        <hr className="hair grow" />
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
          run #{run.id} - {run.filename}
        </span>
      </div>
      {report === null ? (
        // status is complete but nothing came back: say so plainly rather than
        // render an empty report as if it were a clean bill.
        <p className="mt-3" style={{ color: "var(--ink-soft)", fontSize: "0.95rem" }}>
          This run finished without a report. Nothing was analyzed, so nothing here says the
          document is clean - re-run it to try again.
        </p>
      ) : (
        <ReportBody report={report} pageCount={run.page_count} />
      )}
    </section>
  );
}

function ReportBody({ report, pageCount }: { report: FaultReport; pageCount: number | null }) {
  const faults = report.faults ?? [];
  const parseFailures = report.parse_failures ?? [];
  const domains = report.domains_checked ?? [];
  return (
    <>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
          {faults.length} {faults.length === 1 ? "candidate fault" : "candidate faults"}
        </span>
        {pageCount !== null && (
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
            {pageCount} {pageCount === 1 ? "page" : "pages"} read
          </span>
        )}
        {report.analysis_seconds > 0 && (
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            {report.analysis_seconds.toFixed(1)}s
          </span>
        )}
        {report.job_id && (
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            job {report.job_id}
          </span>
        )}
      </div>

      {domains.length > 0 && (
        <p className="code mt-2" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
          Checked: {domains.map(humanize).join(", ")}
        </p>
      )}

      {faults.length === 0 && (
        <p className="mt-4" style={{ color: "var(--ink-soft)", fontSize: "0.95rem" }}>
          No candidate deficiencies were found in this document. That is not a clearance - it means
          the checks that ran did not fire.
        </p>
      )}

      {TIER_ORDER.map((tier) => {
        const group = faults.filter((f) => f.tier === tier);
        if (group.length === 0) return null;
        return (
          <div key={tier} className="mt-8">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h3 className="kicker" style={{ color: "var(--ink)" }}>
                {TIER_LABEL[tier]}
              </h3>
              <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
                {group.length}
              </span>
              <hr className="hair grow" />
            </div>
            <p className="code mt-1" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
              {TIER_NOTE[tier]}
            </p>
            <div className="mt-3 flex flex-col gap-3">
              {group.map((fault, i) => (
                <FaultCard key={`${tier}-${i}-${fault.title}`} fault={fault} />
              ))}
            </div>
          </div>
        );
      })}

      {parseFailures.length > 0 && (
        <div className="mt-8">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h3 className="kicker" style={{ color: "var(--ink)" }}>
              Needs human review
            </h3>
            <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
              {parseFailures.length}
            </span>
            <hr className="hair grow" />
          </div>
          <p className="code mt-1" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            These layers returned output the parser could not validate. Nothing was inferred from
            them - a human has to read the raw output.
          </p>
          <div className="mt-3 flex flex-col gap-3">
            {parseFailures.map((pf, i) => (
              <ParseFailureCard key={`${pf.layer}-${i}`} failure={pf} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function SeverityChip({ severity }: { severity: Severity }) {
  const style =
    severity === "high"
      ? { color: "var(--oxblood)", background: "var(--oxblood-wash)", borderColor: "var(--oxblood)" }
      : undefined;
  return (
    <span className="chip code" style={style}>
      {severity}
    </span>
  );
}

function FaultCard({ fault }: { fault: Fault }) {
  // Where the finding sits in the document, as far as it is actually known.
  const where = [
    fault.section ? fault.section : null,
    fault.page > 0 ? `p.${fault.page}` : null,
    fault.table_ref ? fault.table_ref : null,
  ]
    .filter(Boolean)
    .join(" - ");
  const precedents = fault.precedents ?? [];
  const guidance = fault.guidance_refs ?? [];
  return (
    <article className="doc doc--pad">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="display" style={{ fontSize: "1.08rem", fontWeight: 600 }}>
          {fault.title}
        </span>
        <span className="chip code" style={{ marginLeft: "auto" }}>
          {humanize(fault.category)}
        </span>
        <SeverityChip severity={fault.severity} />
        <span
          className="chip code"
          // code_verified is the only class an oracle stands behind; give it
          // the gold treatment so it never reads like a model opinion.
          style={
            fault.evidence_class === "code_verified"
              ? { color: "var(--gold-ink)", background: "var(--gold-wash)", borderColor: "var(--gold-deep)" }
              : undefined
          }
        >
          {EVIDENCE_LABEL[fault.evidence_class] ?? humanize(fault.evidence_class)}
        </span>
      </div>

      {fault.detail && (
        <p style={{ margin: "0.6rem 0 0", color: "var(--ink-2)", lineHeight: 1.55 }}>{fault.detail}</p>
      )}

      {fault.evidence && (
        <blockquote className="ref__quote" style={{ marginTop: "0.7rem" }}>
          {fault.evidence}
        </blockquote>
      )}

      {(where || fault.source) && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
          {where && (
            <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
              {where}
            </span>
          )}
          {fault.source && (
            <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
              {fault.source}
            </span>
          )}
        </div>
      )}

      {(fault.novel || fault.out_of_distribution || fault.confidence > 0) && (
        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1">
          {fault.novel && <span className="chip code">novel</span>}
          {fault.out_of_distribution && <span className="chip code">out of distribution</span>}
          {fault.confidence > 0 && (
            <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
              confidence {Math.round(fault.confidence * 100)}%
            </span>
          )}
        </div>
      )}

      {fault.challenge_note && (
        <p className="code mt-2" style={{ fontSize: "0.76rem", color: "var(--oxblood)", lineHeight: 1.5 }}>
          Counter-evidence: {fault.challenge_note}
        </p>
      )}

      {guidance.length > 0 && (
        <p className="code mt-2" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
          Guidance: {guidance.join(", ")}
        </p>
      )}

      {precedents.length > 0 && (
        <details className="mt-3">
          <summary
            className="kicker"
            style={{ cursor: "pointer", fontSize: "0.6rem", color: "var(--ink-faint)" }}
          >
            Precedent - {precedents.length}
          </summary>
          <div className="mt-1">
            {precedents.map((p, i) => (
              <PrecedentRow key={`${p.anda_number}-${i}`} n={i + 1} precedent={p} />
            ))}
          </div>
        </details>
      )}
    </article>
  );
}

function PrecedentRow({ n, precedent }: { n: number; precedent: SimilarDeficiency }) {
  return (
    <div className="ref">
      <span className="ref__no">[{n}]</span>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="ref__src">{precedent.anda_number || "ANDA unknown"}</span>
        {precedent.product_name && <span className="ref__page">- {precedent.product_name}</span>}
        {pctMatch(precedent.similarity_score) && (
          <span className="code" style={{ fontSize: "0.68rem", color: "var(--ink-faint)" }}>
            {pctMatch(precedent.similarity_score)}
          </span>
        )}
      </div>
      {precedent.deficiency_text && (
        <blockquote className="ref__quote">{precedent.deficiency_text}</blockquote>
      )}
    </div>
  );
}

function ParseFailureCard({ failure }: { failure: ParseFailed }) {
  return (
    <div className="stamp">
      <div className="stamp__tag">Needs human review</div>
      <p className="code mt-1" style={{ fontSize: "0.78rem" }}>
        {failure.layer}: {failure.reason}
      </p>
      {failure.validation_error && (
        <p className="code mt-1" style={{ fontSize: "0.74rem" }}>
          {failure.validation_error}
        </p>
      )}
      {failure.raw_output && (
        <details className="mt-2">
          <summary className="kicker" style={{ cursor: "pointer", fontSize: "0.6rem" }}>
            Raw output
          </summary>
          <pre
            className="code mt-1"
            style={{ fontSize: "0.72rem", whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0 }}
          >
            {failure.raw_output}
          </pre>
        </details>
      )}
    </div>
  );
}
