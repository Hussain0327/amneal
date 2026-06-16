"use client";

import { useCallback, useEffect, useState } from "react";

import { useCurrentProduct } from "@/components/CurrentProductProvider";
import { PageHeader } from "@/components/PageHeader";
import { listProducts, watchLatest, type AlertRecord, type ProductRecord } from "@/lib/api";
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

export default function WatchPage() {
  const [alerts, setAlerts] = useState<AlertRecord[] | null>(null);
  const [products, setProducts] = useState<ProductRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [productError, setProductError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    watchLatest()
      .then((d) => setAlerts(d.alerts))
      // Leave `alerts` untouched on failure — an error must not masquerade as a
      // loaded-but-empty feed (the empty state below is gated on !error).
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    setProductError(null);
    listProducts()
      .then((d) => setProducts(d.products))
      .catch((e) => setProductError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

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
          <button className="btn btn--ghost mt-3" type="button" onClick={load}>
            Try again
          </button>
        </div>
      )}

      <section className="rise d3">
        <div className="flex items-baseline gap-3">
          <h2 className="kicker" style={{ color: "var(--ink)" }}>
            Bulletin
          </h2>
          <hr className="hair grow" />
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            {alerts ? `${alerts.length} entries` : "…"}
          </span>
        </div>

        {!error && alerts && alerts.length === 0 && (
          <p className="mt-3" style={{ color: "var(--ink-soft)", fontSize: "0.95rem" }}>
            No alerts yet. New and revised product-specific guidances will appear here as they’re detected.
          </p>
        )}

        <div className="mt-3 flex flex-col gap-3">
          {(alerts ?? []).map((r, i) => (
            <article key={i} className="doc doc--seal doc--pad">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="display" style={{ fontSize: "1.15rem", fontWeight: 600 }}>
                  {str(r.active_ingredient) || "—"}
                </span>
                <span className="chip code">PSG {str(r.listing_appl_no) || "—"}</span>
                {str(r.listing_psg_type) && <span className="chip code">{str(r.listing_psg_type)}</span>}
              </div>
              {str(r.diff_summary) && (
                <p style={{ margin: "0.6rem 0 0", color: "var(--ink-2)", lineHeight: 1.55 }}>{str(r.diff_summary)}</p>
              )}
              <div className="mt-3 flex items-center gap-4">
                {str(r.source_url) && (
                  <a className="link code" style={{ fontSize: "0.76rem" }} href={safeHref(str(r.source_url))} target="_blank" rel="noreferrer">
                    View source ↗
                  </a>
                )}
                {str(r.confidence) && (
                  <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
                    confidence {str(r.confidence)}
                  </span>
                )}
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="mt-10 rise d4">
        <div className="flex items-baseline gap-3">
          <h2 className="kicker" style={{ color: "var(--ink)" }}>
            Watchlist
          </h2>
          <hr className="hair grow" />
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            {products ? `${products.length} products` : "…"}
          </span>
        </div>
        <div className="mt-3">
          {productError && (
            <div className="mb-3 flex items-center gap-3">
              <p className="code" style={{ color: "var(--oxblood)", fontSize: "0.82rem", margin: 0 }}>
                {productError}
              </p>
              <button className="btn btn--ghost" type="button" onClick={load}>
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
      <p style={{ color: "var(--ink-soft)", fontSize: "0.9rem" }}>
        Your watchlist is empty. Scope a product — from the bar, Watch, or White Paper — to start tracking it.
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
          {products.map((p, i) => {
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
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c} className={/no|number|appl|ndc|id/i.test(c) ? "code" : undefined}>
                    {str(p[c])}
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
