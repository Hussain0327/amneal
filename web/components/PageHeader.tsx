import { Wordmark } from "./Wordmark";

// Main-area header: the gold Amneal wordmark, the product name in spaced caps,
// a gold rule beneath, and an optional tagline.
export function PageHeader({ product, tagline }: { product: string; tagline?: string }) {
  return (
    <header className="mb-6">
      <div className="flex items-baseline gap-3 border-b-[3px] border-gold pb-2">
        <Wordmark size="lg" />
        <span className="text-sm font-bold uppercase tracking-[0.32em] text-ink">{product}</span>
      </div>
      {tagline && <p className="mt-2 text-sm text-ink-soft">{tagline}</p>}
    </header>
  );
}
