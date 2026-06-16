"use client";

import { useCurrentProduct } from "./CurrentProductProvider";

// The scoped product, promoted out of the sidebar into a slim strip across the
// top of every surface — so it is always clear all four are working one
// product. On-brand cream/gold and sticky to the top of the canvas; this is the
// product-under-review indicator, not a literal pipeline graphic.
export function ProductScopeBar() {
  const { referenceProductName, applicationNumber, hasProduct, clearProduct } = useCurrentProduct();
  return (
    <div className="scopebar" role="status" aria-live="polite">
      <span className="scopebar__eye">Under review</span>
      {hasProduct ? (
        <>
          {referenceProductName && <span className="scopebar__name">{referenceProductName}</span>}
          {applicationNumber && <span className="scopebar__appl code">{applicationNumber}</span>}
          <button className="scopebar__clear" onClick={clearProduct} aria-label="Clear current product">
            clear
          </button>
        </>
      ) : (
        <span className="scopebar__empty">
          No product scoped — set one on White&nbsp;Paper or Watch to focus all four surfaces.
        </span>
      )}
    </div>
  );
}
