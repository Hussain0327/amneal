// Pins the live-draft typewriter's perceived-latency budget. The pacing
// constants are a product decision, not decoration: prod's median answer is
// ~334 visible chars and the server generates it in ~2.5s, so the painter
// must clear a median answer well inside generation time even when the whole
// thing arrives as one burst. If someone slows the cadence back down, this
// fails instead of silently re-adding seconds of perceived latency.
import { describe, expect, it } from "vitest";

import {
  DRAFT_CATCHUP_THRESHOLD_CHARS,
  DRAFT_CHARS_PER_TICK,
  DRAFT_TICK_MS,
  draftTake,
} from "../lib/draft-pacing";

/** Simulates the drain loop: ms until a one-shot burst is fully painted. */
function msToDrain(chars: number): number {
  let remaining = chars;
  let ticks = 0;
  while (remaining > 0) {
    remaining -= draftTake(remaining);
    ticks += 1;
  }
  return ticks * DRAFT_TICK_MS;
}

describe("draft typewriter pacing", () => {
  it("uses the base cadence while caught up", () => {
    expect(draftTake(1)).toBe(DRAFT_CHARS_PER_TICK);
    expect(draftTake(DRAFT_CATCHUP_THRESHOLD_CHARS)).toBe(DRAFT_CHARS_PER_TICK);
  });

  it("scales the take with the backlog once behind", () => {
    expect(draftTake(DRAFT_CATCHUP_THRESHOLD_CHARS + 1)).toBeGreaterThanOrEqual(
      DRAFT_CHARS_PER_TICK,
    );
    expect(draftTake(3000)).toBeGreaterThan(draftTake(300));
  });

  it("paints a median 334-char answer within 1.2s even as one burst", () => {
    expect(msToDrain(334)).toBeLessThanOrEqual(1200);
  });

  it("keeps the base cadence at or above 300 chars/s", () => {
    const charsPerSecond = (DRAFT_CHARS_PER_TICK / DRAFT_TICK_MS) * 1000;
    expect(charsPerSecond).toBeGreaterThanOrEqual(300);
  });
});
