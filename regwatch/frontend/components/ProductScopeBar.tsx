"use client";

import { useState } from "react";

import { ApiError, resolveProduct } from "@/lib/api";
import { useCurrentProduct } from "./CurrentProductProvider";

// The scoped product, promoted out of the sidebar into a slim sticky strip
// across the top of every surface — and the front-door SETTER for the whole
// pipeline. Pinning runs the same deterministic resolve the White Paper uses
// (POST /resolve), so the scope is always canonical and a 422 leaves it unset:
// refuse over guess. This is the product-under-review indicator, not a literal
// pipeline graphic.
export function ProductScopeBar() {
  const { referenceProductName, applicationNumber, hasProduct, clearProduct, setProduct } =
    useCurrentProduct();
  const [open, setOpen] = useState(false);
  const [rld, setRld] = useState("");
  const [appl, setAppl] = useState("");
  const [pinning, setPinning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function openPicker() {
    // Prefill when changing an existing scope; blank when setting fresh.
    setRld(referenceProductName);
    setAppl(applicationNumber);
    setError(null);
    setOpen(true);
  }

  function cancel() {
    setOpen(false);
    setError(null);
  }

  async function onPin(e: React.FormEvent) {
    e.preventDefault();
    const r = rld.trim();
    const a = appl.trim();
    if (!r || !a || pinning) return;
    setPinning(true);
    setError(null);
    try {
      const spine = await resolveProduct(r, a);
      // Only canonical, resolved values become the scope — a 422 throws above,
      // so an unresolvable pair never gets pinned. A resolved application can
      // still lack a canonical ingredient name; fall back to what the analyst
      // typed so the scope is never number-only.
      setProduct({
        referenceProductName: spine.normalized_name || r,
        applicationNumber: spine.application_number,
      });
      setOpen(false);
    } catch (er) {
      // The 422 detail is the resolver's explanation of what WAS found — show
      // it verbatim and set NO scope. Other failures surface the same way.
      setError(
        er instanceof ApiError
          ? er.detail || "Could not resolve that product."
          : er instanceof Error
            ? er.message
            : String(er),
      );
    } finally {
      setPinning(false);
    }
  }

  return (
    // The polite live region covers ONLY the read-only summary states (pinned
    // name / empty). While the picker form is open the region is disabled —
    // otherwise every mutation inside it (the Pin→"Resolving…" label swap,
    // error text, the autofocused inputs) queues screen-reader announcements
    // over the user's own typing echo. The resolve error announces via its
    // own role="alert" below instead.
    <div
      className="scopebar"
      role={open ? undefined : "status"}
      aria-live={open ? undefined : "polite"}
    >
      <span className="scopebar__eye">Under review</span>
      {open ? (
        <form
          onSubmit={onPin}
          className="scopebar__picker"
          onKeyDown={(e) => {
            if (e.key === "Escape" && !pinning) cancel();
          }}
        >
          <input
            className="field scopebar__field"
            value={rld}
            onChange={(e) => setRld(e.target.value)}
            placeholder="Reference product"
            aria-label="Reference product name"
            autoFocus
          />
          <input
            className="field scopebar__field"
            value={appl}
            onChange={(e) => setAppl(e.target.value)}
            placeholder="Application number"
            aria-label="Application number"
          />
          <button className="btn" type="submit" disabled={pinning}>
            {pinning ? "Resolving…" : "Pin"}
          </button>
          <button type="button" className="scopebar__clear" onClick={cancel} disabled={pinning}>
            cancel
          </button>
          {error && (
            <span className="scopebar__error code" role="alert">
              {error}
            </span>
          )}
        </form>
      ) : hasProduct ? (
        <>
          {referenceProductName && <span className="scopebar__name">{referenceProductName}</span>}
          {applicationNumber && <span className="scopebar__appl code">{applicationNumber}</span>}
          <button className="scopebar__clear" onClick={openPicker}>
            change
          </button>
          <button className="scopebar__clear" onClick={clearProduct} aria-label="Clear current product">
            clear
          </button>
        </>
      ) : (
        <>
          <span className="scopebar__empty">No product scoped —</span>
          <button className="scopebar__clear" onClick={openPicker}>
            pin one here
          </button>
          <span className="scopebar__empty">or set one on White&nbsp;Paper or Watch.</span>
        </>
      )}
    </div>
  );
}
