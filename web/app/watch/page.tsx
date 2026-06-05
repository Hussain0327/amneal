"use client";

import { useEffect, useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import {
  listProducts,
  watchLatest,
  type AlertRecord,
  type ProductRecord,
} from "@/lib/api";

function str(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

export default function WatchPage() {
  const [alerts, setAlerts] = useState<AlertRecord[] | null>(null);
  const [products, setProducts] = useState<ProductRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    watchLatest()
      .then((d) => setAlerts(d.alerts))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    listProducts()
      .then((d) => setProducts(d.products))
      .catch(() => setProducts([]));
  }, []);

  return (
    <div className="max-w-4xl">
      <PageHeader
        product="Watch"
        tagline="Recent alerts from the change feed. The watchlist drives what surfaces here."
      />

      {error && (
        <div className="amneal-card mb-4 text-sm text-red-700" style={{ borderLeftColor: "#dc2626" }}>
          {error}
        </div>
      )}

      {alerts && alerts.length === 0 && (
        <p className="text-sm text-ink-soft">
          No alerts yet. Run an ingest cycle and let the matcher build alerts against your watchlist.
        </p>
      )}

      <div className="space-y-3">
        {(alerts ?? []).map((r, i) => (
          <div key={i} className="amneal-card">
            <div className="font-semibold">
              {str(r.active_ingredient)} — PSG {str(r.listing_appl_no)} ({str(r.listing_psg_type)})
            </div>
            {str(r.diff_summary) && (
              <blockquote className="mt-1 border-l-2 border-gold-soft pl-3 text-sm text-ink-soft">
                {str(r.diff_summary)}
              </blockquote>
            )}
            <div className="mt-2 text-sm">
              {str(r.source_url) && (
                <a
                  className="font-medium text-gold-deep underline"
                  href={str(r.source_url)}
                  target="_blank"
                  rel="noreferrer"
                >
                  Source
                </a>
              )}
              {str(r.confidence) && (
                <span className="ml-2 text-ink-soft">confidence {str(r.confidence)}</span>
              )}
            </div>
          </div>
        ))}
      </div>

      <h2 className="mb-2 mt-8 text-xl font-bold text-ink">Watchlist</h2>
      <WatchlistTable products={products} />
    </div>
  );
}

function WatchlistTable({ products }: { products: ProductRecord[] | null }) {
  if (products === null) return <p className="text-sm text-ink-soft">Loading…</p>;
  if (products.length === 0)
    return (
      <p className="text-sm text-ink-soft">
        Watchlist is empty. Add products via the API or <code>regwatch watchlist add</code>.
      </p>
    );
  const columns = Array.from(new Set(products.flatMap((p) => Object.keys(p))));
  return (
    <div className="overflow-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c} className="border border-gold-soft bg-gold-soft px-2 py-1 text-left">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {products.map((p, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c} className="border border-gold-soft px-2 py-1">
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
