"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useCurrentProduct } from "@/components/CurrentProductProvider";
import { PageHeader } from "@/components/PageHeader";
import { AlertEntry, alertKind, fmtDetected, parseUtcMs } from "@/components/WatchEntry";
import {
  listProducts,
  watchLatest,
  type ProductRecord,
  type WatchLatest,
  type WatchRunSummary,
} from "@/lib/api";
import { safeHref } from "@/lib/url";

function str(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

// Canonical application number: digits only, zero-padded to six — mirroring the
// backend's clean_application_number (NDA/ANDA prefix stripped). The watchlist
// stores the raw FDA value (e.g. "NDA020503"); canonicalizing here makes a row
// scoped from Watch write — and match — the SAME value the bar / White Paper pin.
function canonAppl(v: unknown): string {
  const digits = str(v).replace(/\D/g, "");
  return digits ? digits.padStart(6, "0") : "";
}

// parseUtcMs / fmtDetected / alertKind moved to components/WatchEntry.tsx with
// the entry markup; the page still consumes them for run freshness and the
// kind facet, so the filter can never disagree with the stamp on a card.

// The watch cron runs daily, so a finished_at older than two scheduled runs
// means at least one run failed to record and the bulletin can no longer be
// presumed current. Run recency is deliberately separate from the alert list's
// emptiness: an empty feed under a fresh run means "nothing changed", not
// "nothing checked".
const RUN_STALE_AFTER_MS = 48 * 60 * 60 * 1000;

// Bulletin filter facet. "All" is a UI state, not a wire value -- the wire
// only knows new/revised (via alertKind's structural-first classification).
const KIND_FILTERS = ["All", "New", "Revised"] as const;
type KindFilter = (typeof KIND_FILTERS)[number];

// asOfMs is the moment the feed was FETCHED, not render time: render must stay
// pure (react-hooks/purity forbids Date.now() here), and the page refetches on
// tab focus, so a long-idle tab re-judges staleness the moment it returns.
function RunFreshness({ lastRun, asOfMs }: { lastRun: WatchRunSummary | null; asOfMs: number }) {
  if (lastRun === null) {
    // No run has ever recorded (fresh install / wiped ledger). Say so plainly
    // instead of implying a check happened (INV-4: never report a run that
    // did not happen).
    return <p className="issue-line">No watch run recorded yet.</p>;
  }
  const finished = str(lastRun.finished_at);
  const when = fmtDetected(finished);
  const finishedMs = parseUtcMs(finished);
  // NaN compares false on both sides, so an unparseable finished_at never
  // claims a staleness it cannot prove; the date itself falls back to an
  // honest placeholder rather than "Invalid Date".
  const stale = Number.isFinite(finishedMs) && asOfMs - finishedMs > RUN_STALE_AFTER_MS;
  return (
    <p className="issue-line">
      Last checked {when ? <time dateTime={finished}>{when}</time> : "(date unavailable)"}.
      {stale && (
        <span className="issue-line__warn">
          {" "}
          More than 48 hours ago; the feed may be out of date.
        </span>
      )}
      {lastRun.errors > 0 && (
        <span className="issue-line__warn">
          {" "}
          The last run hit {lastRun.errors} ingest {lastRun.errors === 1 ? "error" : "errors"};
          the bulletin may be incomplete.
        </span>
      )}
    </p>
  );
}

export default function WatchPage() {
  // The whole /watch/latest payload, not just the alert page: last_run drives
  // the freshness line and total drives the "showing newest N of M" honesty
  // line, so throwing either away here would force the page to guess.
  const [feed, setFeed] = useState<WatchLatest | null>(null);
  // Wall-clock time the feed landed, for the staleness judgement (see
  // RunFreshness): set together with `feed`, never read before it.
  const [feedAt, setFeedAt] = useState(0);
  const [products, setProducts] = useState<ProductRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [productError, setProductError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [kindFilter, setKindFilter] = useState<KindFilter>("All");
  // Scoping from an alert card writes the same canonical pair the watchlist
  // table below writes (see WatchlistTable); the hook is read here because the
  // cards render in this component.
  const { applicationNumber, referenceProductName, setProduct } = useCurrentProduct();
  // Serializes loads: the mount effect, Refresh, and the focus refetch can all
  // fire; while one load is outstanding the others are no-ops (see load()).
  const loadingRef = useRef(false);

  const load = useCallback(() => {
    // In-flight guard: collapse overlapping loads so a burst of tab-focus events
    // cannot hammer the API, a tab-return's focus+visibilitychange pair fires one
    // load not two, and only one request set is ever outstanding -- so an older
    // response can never overwrite a newer feed (no last-write-wins race).
    if (loadingRef.current) return;
    loadingRef.current = true;
    setBusy(true);
    setError(null);
    setProductError(null);
    const alertsDone = watchLatest()
      .then((d) => {
        setFeed(d);
        setFeedAt(Date.now());
      })
      // Leave `feed` untouched on failure — an error must not masquerade as a
      // loaded-but-empty feed (the empty state below is gated on !error).
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    const productsDone = listProducts()
      .then((d) => setProducts(d.products))
      .catch((e) => setProductError(e instanceof Error ? e.message : String(e)));
    void Promise.allSettled([alertsDone, productsDone]).then(() => {
      loadingRef.current = false;
      setBusy(false);
    });
  }, []);

  useEffect(() => {
    // Fetch the feed on mount. load() guards against re-entry, so a StrictMode
    // double-invoke or an overlapping focus refetch collapses to a single request.
    load();
  }, [load]);

  useEffect(() => {
    // Auto-refetch when the tab regains focus, so a long-open bulletin does not
    // sit silently stale. load() is a stable useCallback (deps []), so this
    // listener attaches once; the refetch runs from the event, not on render.
    const refetchOnVisible = () => {
      if (document.visibilityState === "visible") load();
    };
    window.addEventListener("focus", refetchOnVisible);
    document.addEventListener("visibilitychange", refetchOnVisible);
    return () => {
      window.removeEventListener("focus", refetchOnVisible);
      document.removeEventListener("visibilitychange", refetchOnVisible);
    };
  }, [load]);

  const alerts = feed ? feed.alerts : null;
  // The facet runs over the SAME classifier the cards render (alertKind), so
  // the filter can never disagree with the kind chip printed on a card --
  // structural change_kind first, prose-marker fallback included.
  const visible = (alerts ?? []).filter((r) => kindFilter === "All" || alertKind(r) === kindFilter);

  return (
    <div className="measure">
      <PageHeader
        index="03"
        product="Watch"
        title="The change bulletin."
        tagline="Recent movements in the guidance feed, matched against your watchlist. New and revised product-specific guidances surface here as they're detected."
      />

      {error && (
        <div className="stamp rise d3">
          <div className="stamp__tag">Feed unavailable</div>
          <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
            {error}
          </p>
          {/* disabled while a load is in flight: load() silently no-ops behind
              the loadingRef guard, so an enabled button here would be a dead
              retry for up to the sibling request's full timeout. */}
          <button className="btn btn--ghost mt-3" type="button" onClick={load} disabled={busy}>
            Try again
          </button>
        </div>
      )}

      <section className="rise d3" aria-busy={busy}>
        <div className="flex items-baseline gap-3">
          <h2 className="kicker" style={{ color: "var(--ink)" }}>
            Bulletin
          </h2>
          <hr className="hair grow" />
          <span className="code count-line">
            {/* When the facet hides rows the counter must say so ("3 of 12
                entries"), never pretend the visible slice is the whole page. */}
            {error
              ? "—"
              : alerts
                ? visible.length === alerts.length
                  ? `${alerts.length} entries`
                  : `${visible.length} of ${alerts.length} entries`
                : "…"}
          </span>
          <button
            className="btn btn--ghost"
            type="button"
            onClick={load}
            disabled={busy}
            style={{ padding: "0.32rem 0.7rem", fontSize: "0.62rem", opacity: busy ? 0.6 : 1 }}
          >
            {busy ? "Refreshing" : "Refresh"}
          </button>
        </div>

        {/* Run recency, not list emptiness: rendered whenever the feed loaded,
            alerts or none, so "quiet feed" and "watch not running" stay
            distinguishable. `?? null` guards a backend that predates last_run
            (absent field must read as "no run recorded", not crash). */}
        {!error && feed && <RunFreshness lastRun={feed.last_run ?? null} asOfMs={feedAt} />}

        {!error && alerts && alerts.length === 0 && (
          <p className="bulletin-empty">
            No alerts yet. New and revised product-specific guidances will appear here as they’re detected.
          </p>
        )}

        {!error && alerts && alerts.length > 0 && (
          <div className="mt-3 facet" role="group" aria-label="Filter bulletin by change kind">
            {KIND_FILTERS.map((k) => (
              <button
                key={k}
                type="button"
                className="facet__btn"
                aria-pressed={kindFilter === k}
                onClick={() => setKindFilter(k)}
              >
                {k}
              </button>
            ))}
          </div>
        )}

        {/* A facet with zero matches must not read as an empty feed -- the
            alerts exist, this page just has none of the selected kind. */}
        {!error && alerts && alerts.length > 0 && visible.length === 0 && (
          <p className="bulletin-empty">
            No {kindFilter === "New" ? "new" : "revised"} entries in the current feed.
          </p>
        )}

        {/* On a failed (re)load the error stamp above owns the display — don't
            also render a now-stale bulletin beneath it. The feed renders as
            ONE ruled sheet (see .bulletin/.entry), not a stack of cards. */}
        {!error && visible.length > 0 && (
          <div className="doc bulletin">
            {visible.map((r) => {
              // Scoping from an alert writes the SAME canonical pair the
              // watchlist rows and the top bar pin (name + six-digit appl),
              // so a product scoped here matches everywhere downstream.
              // active_ingredient is the only name the alert wire carries.
              const name = str(r.active_ingredient);
              const appl = canonAppl(r.listing_appl_no);
              const scopeable = Boolean(name || appl);
              // Scoped iff this card's (name, appl) identity IS the active
              // scope -- both halves, exactly what the button writes (the
              // same rule WatchlistTable applies to its rows).
              const scoped =
                scopeable && name === referenceProductName && appl === applicationNumber;
              return (
                <AlertEntry
                  key={`${r.psg_document_id}-${r.psg_version_id}-${r.product_id}`}
                  alert={r}
                  scopeable={scopeable}
                  scoped={scoped}
                  onScope={() => setProduct({ referenceProductName: name, applicationNumber: appl })}
                />
              );
            })}
          </div>
        )}

        {/* The page is a newest-first window, not the whole ledger; when rows
            exist past this page say so instead of pretending completeness.
            Uses the unfiltered page size on purpose -- the facet counter above
            already owns the filtered story. */}
        {!error && feed && feed.total > feed.alerts.length && (
          <p className="code count-line mt-3">
            Showing newest {feed.alerts.length} of {feed.total} entries.
          </p>
        )}
      </section>

      <section className="mt-10 rise d4">
        <div className="flex items-baseline gap-3">
          <h2 className="kicker" style={{ color: "var(--ink)" }}>
            Watchlist
          </h2>
          <hr className="hair grow" />
          <span className="code count-line">
            {productError ? "—" : products ? `${products.length} products` : "…"}
          </span>
        </div>
        <div className="mt-3">
          {productError && (
            <div className="mb-3 flex items-center gap-3">
              <p className="code" style={{ color: "var(--oxblood)", fontSize: "0.82rem", margin: 0 }}>
                {productError}
              </p>
              {/* Same dead-retry guard as the feed stamp's Try again above. */}
              <button className="btn btn--ghost" type="button" onClick={load} disabled={busy}>
                Try again
              </button>
            </div>
          )}
          <WatchlistTable products={products} error={productError} />
        </div>
      </section>
    </div>
  );
}

// Humanized headers for the ledger — the table reads object keys for data, but
// shows analyst-facing labels rather than raw snake_case column names.
const COLUMN_LABELS: Record<string, string> = {
  active_ingredient: "Active ingredient",
  dosage_form: "Dosage form",
  route: "Route",
  rld_name: "RLD name",
  rld_application_number: "Application no.",
  company_status: "Company status",
  source: "Source",
  source_url: "Source URL",
};

function WatchlistTable({ products, error }: { products: ProductRecord[] | null; error: string | null }) {
  // Watch is the second place a product can be scoped: a watchlist row carries
  // both halves (rld_name + rld_application_number), so scoping from here is a
  // faithful set, not a guess.
  const { applicationNumber, referenceProductName, setProduct } = useCurrentProduct();
  // The error (with its own retry) is shown above the table — don't also render
  // a misleading "Loading…" / empty message under it.
  if (error) return null;
  if (products === null) return <p style={{ color: "var(--ink-soft)", fontSize: "0.9rem" }}>Loading…</p>;
  if (products.length === 0)
    return (
      // Truthful about population: rows come from the automated Drugs@FDA
      // import or the products API. Scoping (bar / Watch / White Paper) only
      // sets rp/appl page context and never writes a watchlist row, so the
      // copy must not promise that scoping "starts tracking" anything.
      <p style={{ color: "var(--ink-soft)", fontSize: "0.9rem" }}>
        Your watchlist is empty. Products are added by the automated Drugs@FDA import or through the
        products API; scoping a product sets the page context but does not add it here.
      </p>
    );
  const columns: Array<keyof ProductRecord> = [
    "active_ingredient",
    "dosage_form",
    "route",
    "rld_name",
    "rld_application_number",
    "company_status",
    "source",
    "source_url",
  ];
  return (
    <div className="doc" style={{ overflow: "auto" }}>
      <table className="ledger">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{COLUMN_LABELS[c] ?? c}</th>
            ))}
            <th aria-label="scope" />
          </tr>
        </thead>
        <tbody>
          {products.map((p) => {
            // Canonical name + number, so a product scoped from here is the
            // SAME rp=/appl= pair the top bar and White Paper pin (all three
            // write the normalized name and the six-digit application number).
            const appl = canonAppl(p.rld_application_number);
            const name = str(p.normalized_name) || str(p.rld_name) || str(p.active_ingredient);
            // Nothing to scope to unless the row names a reference product.
            const scopeable = Boolean(appl || str(p.rld_name) || str(p.normalized_name));
            // Scoped iff this row's (name, appl) identity IS the active scope —
            // both halves, matching exactly what the button writes. Keys on the
            // pair so duplicate ANDAs sharing one RLD don't all light up, and a
            // name-only row still reflects correctly (its appl is "" on both sides).
            const scoped = scopeable && name === referenceProductName && appl === applicationNumber;
            return (
              <tr key={p.id ?? `${appl}|${str(p.active_ingredient)}|${str(p.dosage_form)}`}>
                {columns.map((c) => (
                  <td key={c} className={/no|number|appl|ndc|id/i.test(c) ? "code" : undefined}>
                    {/* A raw URL blows the column out; the ledger shows a
                        compact link instead (guarded like every source href). */}
                    {c === "source_url" && str(p[c]) ? (
                      <a
                        className="link code"
                        style={{ fontSize: "0.74rem" }}
                        href={safeHref(str(p[c]))}
                        target="_blank"
                        rel="noreferrer"
                      >
                        source ↗
                      </a>
                    ) : (
                      str(p[c])
                    )}
                  </td>
                ))}
                <td>
                  {scopeable && (
                    <button
                      className="chip"
                      aria-pressed={scoped}
                      onClick={() => setProduct({ referenceProductName: name, applicationNumber: appl })}
                    >
                      {scoped ? "scoped" : "scope"}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
