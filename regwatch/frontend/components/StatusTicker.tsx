"use client";

// The docket log rendered while a query is in flight: one line per SSE status
// frame, latest line live (gold rule draws in, seal-dot pulses), superseded
// lines dimmed in place. Falls back to a single generic line when the backend
// streams nothing (plain /query fallback, or no frames yet).
export function StatusTicker({ frames }: { frames: string[] }) {
  const lines = frames.length > 0 ? frames : ["Consulting the corpus…"];
  return (
    <div className="ticker" role="status" aria-live="polite">
      {lines.map((text, i) => {
        const live = i === lines.length - 1;
        return (
          <div key={`${i}-${text}`} className={`ticker__line ${live ? "ticker__line--live" : "ticker__line--past"}`}>
            <span className="ticker__rule" aria-hidden />
            <span>{text}</span>
            {live && <span className="ticker__dot" aria-hidden />}
          </div>
        );
      })}
    </div>
  );
}
