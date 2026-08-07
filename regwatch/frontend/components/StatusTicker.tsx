"use client";

// The docket log rendered while a query is in flight: one line per SSE status
// frame, latest line live (gold rule draws in, seal-dot pulses), superseded
// lines dimmed in place. Falls back to a single generic line when the backend
// streams nothing (plain /query fallback, or no frames yet).
export function StatusTicker({ frames }: { frames: string[] }) {
  const lines = frames.length > 0 ? frames : ["Consulting the corpus…"];
  // aria-atomic="false" + aria-hidden on superseded lines means a screen
  // reader announces only the newest frame, not the whole accumulated log
  // re-read from the top on every status update.
  return (
    <div className="ticker" role="status" aria-live="polite" aria-atomic="false">
      {lines.map((text, i) => {
        const live = i === lines.length - 1;
        return (
          <div
            key={`${i}-${text}`}
            className={`ticker__line ${live ? "ticker__line--live" : "ticker__line--past"}`}
            aria-hidden={live ? undefined : true}
          >
            {/* Step ordinal, zero-padded like the provenance docket log these
                same frames settle into -- the live run and the kept record
                share one numbering. */}
            <span className="ticker__no" aria-hidden>
              {String(i + 1).padStart(2, "0")}
            </span>
            <span className="ticker__rule" aria-hidden />
            <span>{text}</span>
            {live && <span className="ticker__dot" aria-hidden />}
          </div>
        );
      })}
    </div>
  );
}
