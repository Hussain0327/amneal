import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, askQuery, me } from "@/lib/api";

// Finding frontend-ui-2: every frontend fetch must carry a timeout so a hung
// backend can't leave the page spinning forever. These tests pin that the
// shared fetch wrappers (a) reject with the defined ApiError when their timer
// fires, and (b) still honor an explicit caller signal (user-cancel) instead
// of swallowing it as a timeout.

// A fetch that never resolves AND respects an AbortSignal: it rejects with the
// real DOMException("AbortError") the moment the passed signal aborts. That is
// exactly what a hung-then-aborted browser fetch does, so the wrapper's
// abort-vs-timeout discrimination is exercised for real.
function hangingFetch(): typeof fetch {
  return vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    return new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) return; // never settles
      if (signal.aborted) {
        reject(new DOMException("aborted", "AbortError"));
        return;
      }
      signal.addEventListener(
        "abort",
        () => reject(new DOMException("aborted", "AbortError")),
        { once: true },
      );
    });
  }) as unknown as typeof fetch;
}

describe("fetch wrapper timeout (finding frontend-ui-2)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hangingFetch());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("rejects a JSON GET with the timeout ApiError once the default bound elapses", async () => {
    const promise = me();
    // Attach the rejection handler before advancing time so the eventual
    // rejection is always observed (no unhandled-rejection warning).
    const assertion = expect(promise).rejects.toMatchObject({
      name: "ApiError",
      status: 504,
    });
    // Just under the 30s default: still pending.
    await vi.advanceTimersByTimeAsync(29_000);
    // Crossing the bound fires the timer -> AbortError -> mapped to ApiError.
    await vi.advanceTimersByTimeAsync(2_000);
    await assertion;
    await expect(promise).rejects.toBeInstanceOf(ApiError);
  });

  it("propagates an explicit caller abort as an AbortError, not a timeout", async () => {
    const controller = new AbortController();
    const promise = askQuery("q", null, null, controller.signal);
    const assertion = expect(promise).rejects.toMatchObject({ name: "AbortError" });
    // User cancels well before the timeout bound.
    controller.abort();
    await assertion;
    // It must NOT have been mapped to the timeout ApiError.
    await expect(promise).rejects.not.toBeInstanceOf(ApiError);
  });

  it("does not fire the timeout for a caller-aborted call even after the bound elapses", async () => {
    const controller = new AbortController();
    const promise = askQuery("q", null, null, controller.signal);
    const assertion = expect(promise).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await assertion;
    // Advancing past the bound must not produce a second/leaked rejection or a
    // dangling timer doing work after the request already settled.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(vi.getTimerCount()).toBe(0);
  });
});
