"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { useCurrentProduct } from "@/components/CurrentProductProvider";
import { PageHeader } from "@/components/PageHeader";
import {
  ApiError,
  buildWhitepaper,
  clearWhitepaperInput,
  deleteWhitepaperRun,
  downloadWhitepaperDocx,
  finalizeWhitepaperRun,
  getWhitepaperRun,
  listWhitepaperRuns,
  reopenWhitepaperRun,
  saveWhitepaperInput,
  type WhitepaperCell,
  type WhitepaperCellMode,
  type WhitepaperCellStatus,
  type WhitepaperEvidence,
  type WhitepaperInput,
  type WhitepaperResponse,
  type WhitepaperRunDetail,
  type WhitepaperRunList,
  type WhitepaperRunSummary,
  type WhitepaperSectionData,
  type WhitepaperSpine,
} from "@/lib/api";
import { safeHref } from "@/lib/url";

const MODE_LABEL: Record<WhitepaperCellMode, string> = {
  auto: "auto",
  evidence_only: "evidence",
  manual: "manual",
};

// Text equivalent for the status glyph so the populated/absent/pending state is
// not conveyed by color alone (WCAG 1.1.1 / 1.4.1).
const STATUS_LABEL: Record<WhitepaperCellStatus, string> = {
  populated: "Populated",
  verified_absent: "Verified absent",
  analyst_input_required: "Analyst input required",
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

// Compact relative time for the runs list and input attribution -- the same
// naive-UTC convention as the sidebar history list.
function relTime(iso: string): string {
  const norm = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
  const t = Date.parse(norm);
  if (Number.isNaN(t)) return "";
  const mins = Math.floor(Math.max(0, Date.now() - t) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d`;
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function tally(sections: WhitepaperSectionData[]) {
  let populated = 0;
  let absent = 0;
  let pending = 0;
  for (const section of sections) {
    for (const cell of section.cells) {
      if (cell.status === "populated") populated += 1;
      else if (cell.status === "verified_absent") absent += 1;
      else pending += 1;
    }
  }
  return { populated, absent, pending, total: populated + absent + pending };
}

// Analyst completion over the run's IMMUTABLE generated layer: how many
// analyst-required cells carry an overlay value. Notes on populated cells
// deliberately do not count -- they annotate, they do not complete anything.
function analystProgress(sections: WhitepaperSectionData[], inputs: Record<string, WhitepaperInput>) {
  let required = 0;
  let filled = 0;
  for (const section of sections) {
    for (const cell of section.cells) {
      if (cell.status !== "analyst_input_required") continue;
      required += 1;
      if (inputs[cell.id]) filled += 1;
    }
  }
  return { required, filled };
}

// The workflow layer a cell renders against: the attributed overlay value (if
// any) plus the save/clear handlers. null means the ephemeral inline view (a
// populate whose persist degraded) -- no overlay layer exists there.
interface CellWorkflow {
  input: WhitepaperInput | null;
  // final runs freeze the analyst layer; editors never render.
  frozen: boolean;
  onSave: (cellId: string, value: string) => Promise<void>;
  onClear: (cellId: string) => Promise<void>;
}

export default function WhitepaperPage() {
  // useSearchParams needs a Suspense boundary to prerender cleanly (same
  // pattern as the Ask page).
  return (
    <Suspense fallback={null}>
      <WhitepaperView />
    </Suspense>
  );
}

function WhitepaperView() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const urlRun = searchParams.get("run");

  // The reference product name + application number ARE the scoped product, so
  // this surface both reads it (prefill) and writes it (on a successful
  // resolve). Seed from the URL scope, then keep each field in sync with the
  // scope as it changes in place (a query-only scope change does NOT remount) —
  // but only for fields the analyst hasn't edited, so in-progress typing is
  // never clobbered (the dirty-guard below).
  const { referenceProductName, applicationNumber } = useCurrentProduct();
  const [rld, setRld] = useState(() => referenceProductName);
  const [applNo, setApplNo] = useState(() => applicationNumber);
  const lastScope = useRef({ rld: referenceProductName, applNo: applicationNumber });
  useEffect(() => {
    // Adopt a NEW non-empty scope onto an untouched field; never let a scope
    // *clear* blank a prefilled-but-untouched field (the trailing && guards).
    setRld((cur) => (cur === lastScope.current.rld && referenceProductName ? referenceProductName : cur));
    setApplNo((cur) => (cur === lastScope.current.applNo && applicationNumber ? applicationNumber : cur));
    lastScope.current = { rld: referenceProductName, applNo: applicationNumber };
  }, [referenceProductName, applicationNumber]);

  // Ephemeral populate result: rendered inline ONLY when the persist degraded
  // (run_id null) -- a persisted populate navigates to its ?run= instead.
  const [result, setResult] = useState<WhitepaperResponse | null>(null);
  // 422 (spine could not resolve) is an expected, explanatory outcome and is
  // rendered inline as its own state — distinct from transport/server errors.
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // --- saved run (the ?run= URL param is the state of record) ---
  const [run, setRun] = useState<WhitepaperRunDetail | null>(null);
  const [runLoading, setRunLoading] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  // Mirrors the loaded run id so the URL-sync effect can tell "already showing
  // this run" (skip refetch) from "another run was selected" (fetch) -- the
  // Ask page's sessionIdRef pattern.
  const runIdRef = useRef<number | null>(null);

  // --- runs list (org-shared) ---
  const [runsFeed, setRunsFeed] = useState<WhitepaperRunList | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runsBusy, setRunsBusy] = useState(false);
  const runsLoadingRef = useRef(false);

  // --- run workflow actions ---
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [statusBusy, setStatusBusy] = useState(false);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [finalizeConfirm, setFinalizeConfirm] = useState(false);
  // Delete runs from the list: id pending inline confirm / in-flight / failed.
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null);
  const [deleteBusy, setDeleteBusy] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState<{ id: number; message: string } | null>(null);

  const loadRuns = useCallback(() => {
    // In-flight guard: collapse overlapping loads (mount, Refresh, the
    // focus+visibilitychange pair) into one request -- the Watch page pattern.
    if (runsLoadingRef.current) return;
    runsLoadingRef.current = true;
    setRunsBusy(true);
    setRunsError(null);
    listWhitepaperRuns()
      .then((d) => setRunsFeed(d))
      // Leave runsFeed untouched on failure -- an error must not masquerade as
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

  useEffect(() => {
    // Auto-refetch when the tab regains focus, so a long-open list reflects
    // teammates' runs. loadRuns is a stable useCallback (deps []).
    const refetchOnVisible = () => {
      if (document.visibilityState === "visible") loadRuns();
    };
    window.addEventListener("focus", refetchOnVisible);
    document.addEventListener("visibilitychange", refetchOnVisible);
    return () => {
      window.removeEventListener("focus", refetchOnVisible);
      document.removeEventListener("visibilitychange", refetchOnVisible);
    };
  }, [loadRuns]);

  // Set (or clear) the ?run= param, preserving every other param (rp/appl and
  // anything else). Reads the LIVE URL, not the render-time snapshot, so a
  // product pinned mid-flight is never wiped (the Ask page's session-stamp rule).
  const openRun = useCallback(
    (id: number | null) => {
      const params = new URLSearchParams(window.location.search);
      if (id === null) params.delete("run");
      else params.set("run", String(id));
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname],
  );

  // This effect synchronizes the run view to the URL `run` param: every
  // setState in it is an intentional reset/sync, not a cascading-render bug
  // (same shape and lint carve-out as the Ask page's session-sync effect).
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    const parsed = urlRun === null ? NaN : Number(urlRun);
    if (urlRun === null || !Number.isInteger(parsed) || parsed <= 0) {
      runIdRef.current = null;
      setRun(null);
      setRunError(null);
      setRunLoading(false);
      setFinalizeConfirm(false);
      setStatusError(null);
      setDownloadError(null);
      return;
    }
    if (parsed === runIdRef.current) return;
    let cancelled = false;
    setRunLoading(true);
    setRunError(null);
    setFinalizeConfirm(false);
    setStatusError(null);
    setDownloadError(null);
    getWhitepaperRun(parsed)
      .then((d) => {
        if (cancelled) return;
        runIdRef.current = parsed;
        setRun(d);
      })
      .catch((e) => {
        if (cancelled) return;
        // The load failed: reset identity so retrying (or re-selecting the
        // same run) refetches instead of silently showing the previous run.
        runIdRef.current = null;
        setRun(null);
        setRunError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setRunLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [urlRun]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
      const built = await buildWhitepaper(r, a);
      // One router.replace writes the scope AND the run together. Two writes
      // (setProduct + openRun) would race: each computes from a snapshot that
      // does not include the other's params, so the last one would wipe the
      // first. rp/appl are CurrentProductProvider's own URL keys -- the
      // provider reads them straight from the URL, so writing them here IS
      // setProduct. Only a resolved spine gets this far (a 422 throws above),
      // and the spine's CANONICAL identity is what is pinned.
      const params = new URLSearchParams(window.location.search);
      const rp = built.spine.normalized_name || r;
      const appl = built.spine.application_number;
      if (rp) params.set("rp", rp);
      else params.delete("rp");
      if (appl) params.set("appl", appl);
      else params.delete("appl");
      if (built.run_id !== null && built.run_id !== undefined) {
        // Durable run created: the URL becomes the state of record and the
        // run view hydrates from GET /whitepaper/runs/{id} -- a refresh
        // resumes exactly here.
        setResult(null);
        params.set("run", String(built.run_id));
        loadRuns();
      } else {
        // Persist DEGRADED (run_id null): the populate is complete but not
        // durable. Render it inline -- the backend appended an explicit
        // warning to the result -- and clear any open saved run so the two
        // views cannot stack.
        setResult(built);
        params.delete("run");
      }
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
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

  // The .docx renders server-side FROM the saved run (fingerprint re-verified
  // there): the document always matches the stored generated layer plus the
  // attributed overlay, never client state.
  async function onDownload() {
    if (!run || downloading) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      await downloadWhitepaperDocx(run.id, run.application_number);
    } catch (er) {
      setDownloadError(er instanceof Error ? er.message : String(er));
    } finally {
      setDownloading(false);
    }
  }

  // Refetch the canonical run after a workflow flip: finalized_at/by come
  // from the store, not the status response.
  async function reloadRun(id: number) {
    const fresh = await getWhitepaperRun(id);
    if (runIdRef.current === id) setRun(fresh);
  }

  async function onFinalize() {
    if (!run || statusBusy) return;
    setStatusBusy(true);
    setStatusError(null);
    try {
      await finalizeWhitepaperRun(run.id);
      await reloadRun(run.id);
      setFinalizeConfirm(false);
      loadRuns();
    } catch (er) {
      setStatusError(er instanceof Error ? er.message : String(er));
    } finally {
      setStatusBusy(false);
    }
  }

  async function onReopen() {
    if (!run || statusBusy) return;
    setStatusBusy(true);
    setStatusError(null);
    try {
      await reopenWhitepaperRun(run.id);
      await reloadRun(run.id);
      loadRuns();
    } catch (er) {
      setStatusError(er instanceof Error ? er.message : String(er));
    } finally {
      setStatusBusy(false);
    }
  }

  async function onDelete(id: number) {
    if (deleteBusy !== null) return;
    setDeleteBusy(id);
    setDeleteError(null);
    try {
      await deleteWhitepaperRun(id);
      setDeleteConfirm(null);
      // Deleting the open run closes its view -- the URL-sync effect clears
      // the state when the param goes.
      if (runIdRef.current === id) openRun(null);
      loadRuns();
    } catch (er) {
      // 403 (not the creator) / 409 (finalized) are the server's rules; the
      // client does not know the current user id, so the affordance always
      // shows and the refusal surfaces inline instead.
      setDeleteConfirm(null);
      setDeleteError({ id, message: er instanceof Error ? er.message : String(er) });
    } finally {
      setDeleteBusy(null);
    }
  }

  // Merge a saved/cleared overlay value into the run state so attribution
  // shows immediately -- the server response is the stored truth.
  const applyInput = useCallback((cellId: string, input: WhitepaperInput | null) => {
    setRun((prev) => {
      if (!prev) return prev;
      const inputs = { ...prev.inputs };
      if (input === null) delete inputs[cellId];
      else inputs[cellId] = input;
      return { ...prev, inputs };
    });
  }, []);

  const onSaveInput = useCallback(
    async (cellId: string, value: string) => {
      const id = runIdRef.current;
      if (id === null) return;
      const saved = await saveWhitepaperInput(id, cellId, value);
      // An empty-after-cleaning value clears server-side (cleared=true) --
      // reflect whatever the store actually did.
      if (runIdRef.current === id) applyInput(cellId, saved.input);
    },
    [applyInput],
  );

  const onClearInput = useCallback(
    async (cellId: string) => {
      const id = runIdRef.current;
      if (id === null) return;
      await clearWhitepaperInput(id, cellId);
      if (runIdRef.current === id) applyInput(cellId, null);
    },
    [applyInput],
  );

  const showRunView = urlRun !== null;

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
          <button className="btn" type="submit" disabled={loading || !rld.trim() || !applNo.trim()}>
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

      {/* Ephemeral inline result: ONLY the degraded (run_id null) populate
          path. It was never persisted, so there is no editor layer and no
          .docx render -- the warning in the payload says why. */}
      {result && !loading && !showRunView && (
        <section className="mt-9 rise">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
            <h2 className="kicker" style={{ color: "var(--ink)" }}>
              White paper
            </h2>
            <hr className="hair grow" />
            <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
              audit #{result.audit_id}
            </span>
            <span className="chip code">not saved</span>
          </div>

          <div className="mt-4">
            <SpineCard spine={result.spine} extraWarnings={result.warnings} />
          </div>

          <Tally sections={result.sections} />

          <Sections sections={result.sections} workflow={null} />
        </section>
      )}

      {showRunView && (
        <section className="mt-9 rise" aria-busy={runLoading}>
          {runLoading && (
            <p className="code" style={{ fontSize: "0.74rem", color: "var(--ink-faint)" }}>
              Loading saved run...
            </p>
          )}

          {runError && !runLoading && (
            <div className="stamp rise">
              <div className="stamp__tag">Run unavailable</div>
              <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
                {runError}
              </p>
              <button className="btn btn--ghost mt-3" type="button" onClick={() => openRun(null)}>
                Back to intake
              </button>
            </div>
          )}

          {run && !runLoading && !runError && (
            <RunView
              run={run}
              downloading={downloading}
              downloadError={downloadError}
              statusBusy={statusBusy}
              statusError={statusError}
              finalizeConfirm={finalizeConfirm}
              setFinalizeConfirm={setFinalizeConfirm}
              onDownload={() => void onDownload()}
              onFinalize={() => void onFinalize()}
              onReopen={() => void onReopen()}
              onClose={() => openRun(null)}
              onSaveInput={onSaveInput}
              onClearInput={onClearInput}
            />
          )}
        </section>
      )}

      <section className="mt-10 rise d4">
        <div className="flex items-baseline gap-3">
          <h2 className="kicker" style={{ color: "var(--ink)" }}>
            Saved runs
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
            {runsError ? "-" : runsFeed ? `${runsFeed.runs.length} runs` : "..."}
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

        {!runsError && runsFeed && runsFeed.runs.length === 0 && (
          <p className="mt-3" style={{ color: "var(--ink-soft)", fontSize: "0.95rem" }}>
            No saved runs yet. Populate a white paper above to create the first one; runs are shared
            with every analyst.
          </p>
        )}

        {!runsError && runsFeed && runsFeed.runs.length > 0 && (
          <div className="mt-3 flex flex-col gap-3">
            {runsFeed.runs.map((r) => (
              <RunRow
                key={r.id}
                run={r}
                open={run !== null && run.id === r.id}
                onOpen={() => openRun(r.id)}
                deleteConfirm={deleteConfirm === r.id}
                deleteBusy={deleteBusy === r.id}
                deleteError={deleteError?.id === r.id ? deleteError.message : null}
                onDeleteAsk={() => {
                  setDeleteError(null);
                  setDeleteConfirm(r.id);
                }}
                onDeleteCancel={() => setDeleteConfirm(null)}
                onDeleteConfirm={() => void onDelete(r.id)}
              />
            ))}
          </div>
        )}

        {/* The list is a newest-first window, not the whole ledger. */}
        {!runsError && runsFeed && runsFeed.total > runsFeed.runs.length && (
          <p className="code mt-3" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            Showing newest {runsFeed.runs.length} of {runsFeed.total} runs.
          </p>
        )}
      </section>
    </div>
  );
}

function RunRow({
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
    <article className={`doc doc--pad${open ? " doc--seal" : ""}`} aria-current={open ? "true" : undefined}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {/* The name is the open affordance; delete stays its own button so
            the two actions can never swallow each other. */}
        <button
          type="button"
          className="link display"
          onClick={onOpen}
          style={{ fontSize: "1.1rem", fontWeight: 600, background: "none", border: 0, padding: 0, cursor: "pointer" }}
        >
          {run.ingredient || run.rld_name_input}
        </button>
        <span className="chip code">
          {run.application_type} {run.application_number}
        </span>
        <StatusChip status={run.status} />
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)", marginLeft: "auto" }}>
          {relTime(run.updated_at)}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
          {run.populated_count} populated / {run.verified_absent_count} absent / {run.analyst_input_count} analyst
        </span>
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
          {run.inputs_count} {run.inputs_count === 1 ? "input" : "inputs"}
        </span>
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
          by {run.created_by}
        </span>
        <span style={{ marginLeft: "auto", display: "inline-flex", gap: "0.5rem", alignItems: "center" }}>
          {!deleteConfirm && (
            <button className="chip" type="button" onClick={onDeleteAsk} disabled={deleteBusy}>
              delete
            </button>
          )}
          {deleteConfirm && (
            <>
              <span className="code" style={{ fontSize: "0.7rem", color: "var(--oxblood)" }}>
                Delete this run?
              </span>
              <button className="chip" type="button" onClick={onDeleteConfirm} disabled={deleteBusy}>
                {deleteBusy ? "deleting" : "confirm"}
              </button>
              <button className="chip" type="button" onClick={onDeleteCancel} disabled={deleteBusy}>
                cancel
              </button>
            </>
          )}
        </span>
      </div>
      {deleteError && (
        <p className="code mt-2" style={{ fontSize: "0.74rem", color: "var(--oxblood)" }}>
          Delete failed: {deleteError}
        </p>
      )}
    </article>
  );
}

function StatusChip({ status }: { status: string }) {
  // final borrows the gold treatment so the frozen state is visible at a
  // glance; draft keeps the neutral chip.
  return (
    <span
      className="chip code"
      style={
        status === "final"
          ? { color: "var(--gold-ink)", background: "var(--gold-wash)", borderColor: "var(--gold-deep)" }
          : undefined
      }
    >
      {status}
    </span>
  );
}

function RunView({
  run,
  downloading,
  downloadError,
  statusBusy,
  statusError,
  finalizeConfirm,
  setFinalizeConfirm,
  onDownload,
  onFinalize,
  onReopen,
  onClose,
  onSaveInput,
  onClearInput,
}: {
  run: WhitepaperRunDetail;
  downloading: boolean;
  downloadError: string | null;
  statusBusy: boolean;
  statusError: string | null;
  finalizeConfirm: boolean;
  setFinalizeConfirm: (v: boolean) => void;
  onDownload: () => void;
  onFinalize: () => void;
  onReopen: () => void;
  onClose: () => void;
  onSaveInput: (cellId: string, value: string) => Promise<void>;
  onClearInput: (cellId: string) => Promise<void>;
}) {
  const frozen = run.status === "final";
  const progress = analystProgress(run.sections, run.inputs);
  const workflow = { frozen, onSave: onSaveInput, onClear: onClearInput };
  return (
    <>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <h2 className="kicker" style={{ color: "var(--ink)" }}>
          White paper
        </h2>
        <StatusChip status={run.status} />
        <hr className="hair grow" />
        <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
          run #{run.id} / audit #{run.source_audit_id}
        </span>
        <button className="btn" type="button" onClick={onDownload} disabled={downloading}>
          {downloading ? "Preparing..." : "Download .docx"}
        </button>
        {!frozen && !finalizeConfirm && (
          <button className="btn btn--ghost" type="button" onClick={() => setFinalizeConfirm(true)} disabled={statusBusy}>
            Finalize
          </button>
        )}
        {!frozen && finalizeConfirm && (
          <span className="flex items-center gap-2">
            <span className="code" style={{ fontSize: "0.72rem", color: "var(--oxblood)" }}>
              Freeze analyst edits?
            </span>
            <button className="btn" type="button" onClick={onFinalize} disabled={statusBusy}>
              {statusBusy ? "Finalizing..." : "Confirm finalize"}
            </button>
            <button className="btn btn--ghost" type="button" onClick={() => setFinalizeConfirm(false)} disabled={statusBusy}>
              Cancel
            </button>
          </span>
        )}
        {frozen && (
          <button className="btn btn--ghost" type="button" onClick={onReopen} disabled={statusBusy}>
            {statusBusy ? "Reopening..." : "Reopen"}
          </button>
        )}
        <button className="btn btn--ghost" type="button" onClick={onClose}>
          Close
        </button>
      </div>

      {downloadError && (
        <p className="code mt-2" style={{ fontSize: "0.78rem", color: "var(--oxblood)" }}>
          Download failed: {downloadError}
        </p>
      )}
      {statusError && (
        <p className="code mt-2" style={{ fontSize: "0.78rem", color: "var(--oxblood)" }}>
          {statusError}
        </p>
      )}

      {/* Freshness is honest, not implied: the generated layer is immutable,
          so refreshing data means a NEW run (this one stays). */}
      <p className="code mt-2" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
        Data as of {fmtWhen(run.created_at)} - re-populate to refresh. Created by {run.created_by}.
        {frozen && run.finalized_at && (
          <span>
            {" "}
            Finalized {fmtWhen(run.finalized_at)}
            {run.finalized_by ? ` by ${run.finalized_by}` : ""}.
          </span>
        )}
      </p>

      <div className="mt-4">
        <SpineCard spine={run.spine} extraWarnings={run.warnings} />
      </div>

      <Tally sections={run.sections} />
      <p className="code mt-1" style={{ fontSize: "0.7rem", color: "var(--ink-soft)" }}>
        Analyst progress: {progress.filled} of {progress.required} required cells filled.
      </p>

      {/* key: cells share ids ACROSS runs (the template is fixed), so without
          a remount a run switch would leak one run's unsaved editor text into
          the next run's editor. */}
      <Sections key={run.id} sections={run.sections} workflow={workflow} inputs={run.inputs} />
    </>
  );
}

function Sections({
  sections,
  workflow,
  inputs,
}: {
  sections: WhitepaperSectionData[];
  workflow: Omit<CellWorkflow, "input"> | null;
  inputs?: Record<string, WhitepaperInput>;
}) {
  return (
    <>
      {sections.map((section, i) => (
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
              <Cell
                key={cell.id}
                cell={cell}
                workflow={workflow ? { ...workflow, input: inputs?.[cell.id] ?? null } : null}
              />
            ))}
          </div>
        </section>
      ))}
    </>
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
function Tally({ sections }: { sections: WhitepaperSectionData[] }) {
  const t = tally(sections);
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

function Cell({ cell, workflow }: { cell: WhitepaperCell; workflow: CellWorkflow | null }) {
  const input = workflow?.input ?? null;
  return (
    <div className="wp-cell">
      <div className="wp-cell__head">
        <span className={`wp-dot wp-dot--${cell.status}`} role="img" aria-label={STATUS_LABEL[cell.status]} />
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

      {/* The analyst overlay: attributed human text in a separate layer. On an
          analyst cell it IS the answer (the generated value stays None
          forever, INV-3); on a populated/absent cell it is a NOTE that
          annotates -- the cited value above renders untouched either way. */}
      {workflow && (
        <CellOverlay cell={cell} input={input} workflow={workflow} />
      )}

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

function CellOverlay({
  cell,
  input,
  workflow,
}: {
  cell: WhitepaperCell;
  input: WhitepaperInput | null;
  workflow: CellWorkflow;
}) {
  const isAnalystCell = cell.status === "analyst_input_required";
  const label = isAnalystCell ? "Analyst input" : "Analyst note";
  if (workflow.frozen) {
    // Finalized: the overlay is read-only. Nothing saved renders nothing.
    if (!input) return null;
    return <AnalystValue label={label} input={input} />;
  }
  if (isAnalystCell) {
    // The inline editor is always open on an analyst cell -- filling it is
    // the whole job of the workflow surface.
    return (
      <AnalystEditor
        cellId={cell.id}
        label={label}
        input={input}
        alwaysOpen
        onSave={workflow.onSave}
        onClear={workflow.onClear}
      />
    );
  }
  // Populated / verified-absent: the saved note renders as an annotation and
  // the editor hides behind an explicit affordance.
  return (
    <>
      {input && <AnalystValue label={label} input={input} />}
      <AnalystEditor
        cellId={cell.id}
        label={label}
        input={input}
        alwaysOpen={false}
        onSave={workflow.onSave}
        onClear={workflow.onClear}
      />
    </>
  );
}

function AnalystValue({ label, input }: { label: string; input: WhitepaperInput }) {
  return (
    <div className="wp-analyst">
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
  alwaysOpen,
  onSave,
  onClear,
}: {
  cellId: string;
  label: string;
  input: WhitepaperInput | null;
  alwaysOpen: boolean;
  onSave: (cellId: string, value: string) => Promise<void>;
  onClear: (cellId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(input?.value ?? "");
  const [busy, setBusy] = useState(false);
  const [editorError, setEditorError] = useState<string | null>(null);

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

  if (!alwaysOpen && !open) {
    return (
      <div className="wp-editor">
        <button
          className="chip"
          type="button"
          onClick={() => {
            // Re-seed from the saved value on open so an edit starts from what
            // is stored, not from stale local text.
            setText(input?.value ?? "");
            setEditorError(null);
            setOpen(true);
          }}
        >
          {input ? "Edit note" : "Add note"}
        </button>
      </div>
    );
  }

  return (
    <div className="wp-editor">
      <textarea
        className="field"
        aria-label={`${label} for ${cellId}`}
        value={text}
        onChange={(e) => setText(e.target.value)}
        disabled={busy}
        placeholder={label === "Analyst note" ? "Add an attributed note..." : "Enter the analyst answer..."}
      />
      <div className="mt-2 flex items-center gap-2">
        <button className="btn" type="button" onClick={() => void save()} disabled={busy || !text.trim()}>
          {busy ? "Saving..." : "Save"}
        </button>
        <button
          className="btn btn--ghost"
          type="button"
          onClick={() => void clear()}
          disabled={busy || (!input && !text)}
        >
          Clear
        </button>
        {!alwaysOpen && (
          <button className="btn btn--ghost" type="button" onClick={() => setOpen(false)} disabled={busy}>
            Cancel
          </button>
        )}
      </div>
      {editorError && (
        <p className="code mt-2" style={{ fontSize: "0.74rem", color: "var(--oxblood)" }}>
          Save failed: {editorError}
        </p>
      )}
      {alwaysOpen && input && (
        <p className="wp-analyst__meta">
          Saved by {input.author ?? "unknown"}
          {relTime(input.updated_at) ? ` - ${relTime(input.updated_at)}` : ""}
        </p>
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
        <a className="link code" style={{ fontSize: "0.76rem" }} href={safeHref(ev.source_url)} target="_blank" rel="noreferrer">
          {ev.source_url}
        </a>
      )}
    </div>
  );
}
