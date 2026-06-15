"use client";

import type { Suggestion } from "@/lib/api";

// Two renderings of the same grounded {label, query, filters} payload.
// Chips: the quiet "Continue" row under answers and memos. Options: the
// numbered cards under a refusal — a refusal is a dead end, so its redirects
// get the full clarify-option weight, not a whisper.

export function SuggestionChips({
  heading,
  suggestions,
  onPick,
  busy,
}: {
  heading: string;
  suggestions: Suggestion[];
  onPick: (s: Suggestion) => void;
  busy: boolean;
}) {
  if (suggestions.length === 0) return null;
  return (
    <div className="suggest">
      <span className="kicker" style={{ color: "var(--ink-soft)" }}>
        {heading}
      </span>
      <div className="suggest__chips">
        {suggestions.map((s, i) => (
          <button key={`${s.query}::${i}`} type="button" className="chip" disabled={busy} onClick={() => onPick(s)}>
            {s.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function SuggestionOptions({
  heading,
  suggestions,
  onPick,
  busy,
}: {
  heading: string;
  suggestions: Suggestion[];
  onPick: (s: Suggestion) => void;
  busy: boolean;
}) {
  if (suggestions.length === 0) return null;
  return (
    <div className="suggest">
      <span className="kicker" style={{ color: "var(--gold-ink)" }}>
        {heading}
      </span>
      <div className="suggest__opts">
        {suggestions.map((s, i) => (
          <button key={`${s.query}::${i}`} type="button" className="opt" disabled={busy} onClick={() => onPick(s)}>
            <span className="opt__no">{String(i + 1).padStart(2, "0")}</span>
            <span>{s.label}</span>
            <span className="opt__arrow" aria-hidden>
              →
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
