"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useCurrentProduct } from "@/components/CurrentProductProvider";
import { BlankPaper } from "@/components/whitepaper/BlankPaper";
import { FillMeter, StatusChip, ZoomControl } from "@/components/whitepaper/DocChrome";
import { useFitScale, useNarrow } from "@/components/whitepaper/PaperDoc";
import { RunCard } from "@/components/whitepaper/RunShelf";
import { type DocMeta, WhitePaperDocument } from "@/components/whitepaper/WhitePaperDocument";
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
  type WhitepaperInput,
  type WhitepaperResponse,
  type WhitepaperRunDetail,
  type WhitepaperRunList,
  type WhitepaperSectionData,
} from "@/lib/api";
import { groupCells, tallyGroups } from "@/lib/whitepaper-form";

import "./whitepaper.css";

// How many analyst cells still have nothing in them -- the number the whole
// surface is organised around. Notes on already-cited cells deliberately do not
// count: they annotate, they do not complete anything.
function blanksLeft(sections: WhitepaperSectionData[], inputs: Record<string, WhitepaperInput>) {
  let open = 0;
  let filled = 0;
  for (const section of sections) {
    for (const cell of section.cells) {
      if (cell.status !== "analyst_input_required") continue;
      if (inputs[cell.id]) filled += 1;
      else open += 1;
    }
  }
  return { open, filled };
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
  // scope as it changes in place (a query-only scope change does NOT remount) --
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
  // rendered inline as its own state -- distinct from transport/server errors.
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // The fill-in cascade runs once, on the document the populate produced --
  // held as that document's identity, not a bare flag, so opening a different
  // saved run mid-animation cannot inherit it.
  const [revealFor, setRevealFor] = useState<string | null>(null);

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

  // --- document viewport ---
  const stageRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState<number | null>(null);
  const narrow = useNarrow();
  const scale = useFitScale(stageRef, narrow ? 1 : zoom);
  const [pageCount, setPageCount] = useState(1);
  // Row nodes, so "next blank" can walk the analyst to the cell it names.
  const rowNodes = useRef(new Map<string, HTMLElement>());
  const registerRow = useCallback((cellId: string, el: HTMLElement | null) => {
    if (el) rowNodes.current.set(cellId, el);
    else rowNodes.current.delete(cellId);
  }, []);

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
      setRevealFor(
        built.run_id !== null && built.run_id !== undefined
          ? `run-${built.run_id}`
          : `audit-${built.audit_id}`,
      );
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
  // The one document on the table: a saved run, or an unpersisted populate.
  const doc = useMemo(() => {
    if (run && showRunView) {
      const meta: DocMeta = {
        spine: run.spine,
        warnings: run.warnings,
        auditId: run.source_audit_id,
        runId: run.id,
        status: run.status === "final" ? "final" : "draft",
        preparedBy: run.created_by,
        preparedAt: run.created_at,
        finalizedAt: run.finalized_at,
        finalizedBy: run.finalized_by,
      };
      return { meta, sections: run.sections, inputs: run.inputs };
    }
    if (result && !showRunView) {
      const meta: DocMeta = {
        spine: result.spine,
        warnings: result.warnings,
        auditId: result.audit_id,
        runId: null,
        status: "unsaved",
        preparedBy: null,
        preparedAt: null,
        finalizedAt: null,
        finalizedBy: null,
      };
      return { meta, sections: result.sections, inputs: {} as Record<string, WhitepaperInput> };
    }
    return null;
  }, [result, run, showRunView]);

  // Identity of the document on the table, shared by the remount key and the
  // cascade so the two can never disagree about which document this is.
  const docKey = doc ? (doc.meta.runId !== null ? `run-${doc.meta.runId}` : `audit-${doc.meta.auditId}`) : null;
  const reveal = revealFor !== null && revealFor === docKey;

  // The cascade is a one-shot: it plays on the document that just arrived and
  // never again on a re-render of the same one. The countdown starts when that
  // document is actually on screen -- a persisted populate navigates to ?run=
  // and waits on a fetch, so timing from the submit would burn the window
  // before there was anything to ink in.
  useEffect(() => {
    if (!reveal) return;
    const t = setTimeout(() => setRevealFor(null), 1600);
    return () => clearTimeout(t);
  }, [reveal]);

  const tally = useMemo(
    () => (doc ? tallyGroups(groupCells(doc.sections)) : null),
    [doc],
  );
  const blanks = doc ? blanksLeft(doc.sections, doc.inputs) : { open: 0, filled: 0 };
  const frozen = run?.status === "final";

  // Walk to the next unfilled blank and put the caret in it. Wraps, so the
  // control keeps working on the last one.
  const nextBlank = useCallback(() => {
    if (!doc) return;
    const ids: string[] = [];
    for (const section of doc.sections) {
      for (const cell of section.cells) {
        if (cell.status === "analyst_input_required" && !doc.inputs[cell.id]) ids.push(cell.id);
      }
    }
    if (ids.length === 0) return;
    const active = document.activeElement?.closest?.("[data-cell]") as HTMLElement | null;
    const from = active?.dataset.cell ? ids.indexOf(active.dataset.cell) : -1;
    const target = ids[(from + 1) % ids.length];
    const node = rowNodes.current.get(target);
    if (!node) return;
    // Optional call: jsdom (and any host without smooth scrolling) has no
    // scrollIntoView, and losing the scroll must not lose the focus move.
    node.scrollIntoView?.({ behavior: "smooth", block: "center" });
    node.querySelector("textarea")?.focus({ preventScroll: true });
  }, [doc]);

  return (
    <div className="wp">
      <div className="wp-bar">
        <div className="wp-bar__id">
          <span className="wp-bar__no">04</span>
          <span className="wp-bar__where">White Paper</span>
        </div>

        {doc ? (
          <>
            <div className="wp-bar__doc">
              <span className="wp-bar__name">{doc.meta.spine.ingredient || rld || "White paper"}</span>
              <span className="wp-bar__appl">
                {doc.meta.spine.application_type} {doc.meta.spine.application_number}
              </span>
              <StatusChip status={doc.meta.status === "unsaved" ? "unsaved" : doc.meta.status} />
            </div>
            {tally && <FillMeter tally={tally} filled={blanks.filled} />}
            {!frozen && doc.meta.runId !== null && blanks.open > 0 && (
              <button className="wp-btn wp-btn--ink" type="button" onClick={nextBlank}>
                Next blank ({blanks.open})
              </button>
            )}
          </>
        ) : (
          <p className="wp-bar__lede">
            The CRA template, traced to public FDA records. It cites what it found; it never decides.
          </p>
        )}

        <div className="wp-bar__tools">
          {!narrow && (
            <>
              <ZoomControl zoom={zoom} scale={scale} onZoom={setZoom} />
              <span className="wp-bar__pages">
                {pageCount} {pageCount === 1 ? "page" : "pages"}
              </span>
            </>
          )}
        </div>

        <div className="wp-bar__actions">
          {run && showRunView && !runLoading && (
            <>
              <button className="wp-btn wp-btn--ink" type="button" onClick={() => void onDownload()} disabled={downloading}>
                {downloading ? "Preparing..." : "Download .docx"}
              </button>
              {!frozen && !finalizeConfirm && (
                <button className="wp-btn" type="button" onClick={() => setFinalizeConfirm(true)} disabled={statusBusy}>
                  Finalize
                </button>
              )}
              {!frozen && finalizeConfirm && (
                <>
                  <span className="wp-bar__ask">Freeze analyst edits?</span>
                  <button className="wp-btn wp-btn--danger" type="button" onClick={() => void onFinalize()} disabled={statusBusy}>
                    {statusBusy ? "Finalizing..." : "Confirm finalize"}
                  </button>
                  <button className="wp-btn" type="button" onClick={() => setFinalizeConfirm(false)} disabled={statusBusy}>
                    Cancel
                  </button>
                </>
              )}
              {frozen && (
                <button className="wp-btn" type="button" onClick={() => void onReopen()} disabled={statusBusy}>
                  {statusBusy ? "Reopening..." : "Reopen"}
                </button>
              )}
            </>
          )}
          {(doc || showRunView) && (
            <button className="wp-btn" type="button" onClick={() => { setResult(null); openRun(null); }}>
              New paper
            </button>
          )}
        </div>
      </div>

      {(downloadError || statusError) && (
        <p className="wp-bar__error">{downloadError ? `Download failed: ${downloadError}` : statusError}</p>
      )}

      {resolveError && !loading && (
        <div className="wp-alert wp-alert--resolve">
          <span className="wp-alert__tag">Could not resolve</span>
          <p>{resolveError}</p>
          <p className="wp-alert__foot">Nothing was guessed - check the name/number pair and resubmit.</p>
        </div>
      )}

      {error && !loading && (
        <div className="wp-alert">
          <span className="wp-alert__tag">Request failed</span>
          <p>{error}</p>
        </div>
      )}

      {runError && showRunView && !runLoading && (
        <div className="wp-alert">
          <span className="wp-alert__tag">Run unavailable</span>
          <p>{runError}</p>
          <button className="wp-btn mt-3" type="button" onClick={() => openRun(null)}>
            Back to intake
          </button>
        </div>
      )}

      <div className="wp-viewport" ref={stageRef} aria-busy={runLoading || loading}>
        {runLoading && showRunView && (
          <p className="wp-loading">Loading saved run...</p>
        )}

        {doc && !runLoading && (
          <WhitePaperDocument
            // Cells share ids ACROSS runs (the template is fixed), so a run
            // switch must remount the document or one run's unsaved editor text
            // would leak into the next run's blanks.
            key={docKey ?? "doc"}
            meta={doc.meta}
            sections={doc.sections}
            workflow={
              doc.meta.runId === null
                ? null
                : { frozen: !!frozen, inputs: doc.inputs, onSave: onSaveInput, onClear: onClearInput }
            }
            scale={scale}
            paged={!narrow}
            reveal={reveal}
            onLayout={setPageCount}
            onRegisterRow={registerRow}
          />
        )}

        {!doc && !runLoading && (
          <BlankPaper
            rld={rld}
            applNo={applNo}
            onRld={setRld}
            onApplNo={setApplNo}
            onSubmit={onSubmit}
            loading={loading}
            scale={scale}
            paged={!narrow}
            onLayout={setPageCount}
          />
        )}

        {loading && (
          <p className="wp-working" role="status">
            Resolving the application and querying sources...
          </p>
        )}
      </div>

      <section className="wp-shelf">
        <div className="wp-shelf__head">
          <h2>Saved runs</h2>
          <span className="wp-shelf__count">
            {runsError ? "-" : runsFeed ? `${runsFeed.runs.length} runs` : "..."}
          </span>
          <button className="wp-btn wp-btn--quiet" type="button" onClick={loadRuns} disabled={runsBusy}>
            {runsBusy ? "Refreshing" : "Refresh"}
          </button>
        </div>

        {runsError && (
          <div className="wp-alert">
            <span className="wp-alert__tag">Runs unavailable</span>
            <p>{runsError}</p>
            {/* Disabled while a load is in flight: loadRuns() no-ops behind the
                guard, so an enabled button would be a dead retry. */}
            <button className="wp-btn mt-3" type="button" onClick={loadRuns} disabled={runsBusy}>
              Try again
            </button>
          </div>
        )}

        {!runsError && runsFeed && runsFeed.runs.length === 0 && (
          <p className="wp-shelf__empty">
            No saved runs yet. Fill in the two fields above and populate the first one; runs are
            shared with every analyst.
          </p>
        )}

        {!runsError && runsFeed && runsFeed.runs.length > 0 && (
          <div className="wp-shelf__grid">
            {runsFeed.runs.map((r) => (
              <RunCard
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
          <p className="wp-shelf__more">
            Showing newest {runsFeed.runs.length} of {runsFeed.total} runs.
          </p>
        )}
      </section>
    </div>
  );
}
