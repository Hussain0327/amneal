// Append the scoped-product params to an in-app href so navigating between
// surfaces keeps the current product. `productParams` is "" when unset (see
// CurrentProductProvider.productParams). Shared by the spine rail and the
// history panel so every internal navigation carries scope the same way.
export function withScope(href: string, productParams: string): string {
  if (!productParams) return href;
  return `${href}${href.includes("?") ? "&" : "?"}${productParams}`;
}
