"use client";

import { useEffect, useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { listProducts, watchLatest, type AlertRecord, type ProductRecord } from "@/lib/api";

function str(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

export default function WatchPage() {
  const [alerts, setAlerts] = useState<AlertRecord[] | null>(null);
  const [products, setProducts] = useState<ProductRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [productError, setProductError] = useState<string | null>(null);

  useEffect(() => {
    watchLatest()
      .then((d) => setAlerts(d.alerts))
      .catch((e) => {
        setAlerts([]);
        setError(e instanceof Error ? e.message : String(e));
      });
    listProducts()
      .then((d) => setProducts(d.products))
      .catch((e) => {
        setProducts([]);
        setProductError(e instanceof Error ? e.message : String(e));
      });
  }, []);

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

        {alerts && alerts.length === 0 && (
          <p className="mt-3" style={{ color: "var(--ink-soft)", fontSize: "0.95rem" }}>
            No alerts yet. Run an ingest cycle and let the matcher build alerts against your watchlist.
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
                  <a className="link code" style={{ fontSize: "0.76rem" }} href={str(r.source_url)} target="_blank" rel="noreferrer">
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
            <p className="mb-3 code" style={{ color: "var(--oxblood)", fontSize: "0.82rem" }}>
              {productError}
            </p>
          )}
          <WatchlistTable products={products} />
        </div>
      </section>
    </div>
  );
}

function WatchlistTable({ products }: { products: ProductRecord[] | null }) {
  if (products === null) return <p style={{ color: "var(--ink-soft)", fontSize: "0.9rem" }}>Loading…</p>;
  if (products.length === 0)
    return (
      <p style={{ color: "var(--ink-soft)", fontSize: "0.9rem" }}>
        Watchlist is empty. Add products via the API or <span className="code">regwatch watchlist add</span>.
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
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {products.map((p, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c} className={/no|number|appl|ndc|id/i.test(c) ? "code" : undefined}>
                  {str(p[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
