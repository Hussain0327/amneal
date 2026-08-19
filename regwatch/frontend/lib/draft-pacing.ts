// Client-side typewriter pacing for the live-draft channel (Ask onDraft).
// Adaptive drain, not a fixed rate: base cadence while caught up, and once
// the pending buffer crosses the catch-up threshold the per-tick take scales
// up so the visible text is never more than roughly one catch-up window
// behind the wire -- hungry when behind, calm once caught up.
//
// The base cadence is a perceived-latency budget, not decoration: prod's
// median answer is ~334 visible chars, and the server finishes generating it
// in ~2.5s, so the painter must at least keep pace with generation and clear
// a whole median answer well under the generation time when tokens arrive in
// bursts. test/draftPacing.test.ts pins that budget.
export const DRAFT_TICK_MS = 33; // ~30fps
export const DRAFT_CHARS_PER_TICK = 10; // base cadence (~300 chars/s) once caught up
export const DRAFT_CATCHUP_THRESHOLD_CHARS = 150;
export const DRAFT_CATCHUP_WINDOW_MS = 1000;

/**
 * Chars to drain on this tick given the pending buffer size: the base
 * cadence while caught up; behind the threshold, enough per tick to clear
 * the current backlog within roughly one catch-up window.
 */
export function draftTake(bufferedChars: number): number {
  const ticksToClear = Math.max(1, Math.round(DRAFT_CATCHUP_WINDOW_MS / DRAFT_TICK_MS));
  return bufferedChars > DRAFT_CATCHUP_THRESHOLD_CHARS
    ? Math.max(DRAFT_CHARS_PER_TICK, Math.ceil(bufferedChars / ticksToClear))
    : DRAFT_CHARS_PER_TICK;
}
