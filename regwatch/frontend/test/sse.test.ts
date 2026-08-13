import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { askQueryStream, STREAM_FALLBACK_STATUS } from "@/lib/api";
import type { QueryResponse } from "@/lib/api";

// consumeSse is module-private; its only public driver is askQueryStream, which
// pipes res.body through it. We exercise the REAL parser by mocking global fetch
// to return a Response whose body is a ReadableStream we feed byte-by-byte. The
// SSE contract under test (api.ts consumeSse): zero or more `event: status`
// frames then exactly one `event: result` carrying the full QueryResponse; CRLF
// tolerated; `:` lines are comments/keep-alives; the final record dispatches
// even without its trailing blank-line terminator. A stream that never yields a
// result frame must fall back to plain POST /query, so each test that wants the
// streamed value asserts the result came from the stream (not the fallback).

const enc = new TextEncoder();

// Build a Response whose body streams the given chunks (already split however
// the test wants to simulate read() boundaries). content-type marks it SSE so
// askQueryStream consumes it instead of taking the fallback path.
function sseResponse(chunks: string[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

// A minimal but valid-enough QueryResponse for assertions. consumeSse does not
// inspect the shape (it JSON.parses and returns), so any object round-trips;
// status/answer give us stable fields to assert on.
function resultFrameData(answer: string): QueryResponse {
  return {
    status: "answer",
    answer,
    citations: [],
    clarify: [],
    refused: false,
    session_id: "s1",
  } as unknown as QueryResponse;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// If a test's stream never produces a result, askQueryStream falls back to a
// second fetch (POST /query). Fail loudly so a "passing" test can't actually be
// silently exercising the fallback instead of consumeSse.
function failOnFallback(): void {
  // The first fetch returns the SSE stream; any second fetch is the fallback.
  fetchMock.mockImplementationOnce(() => {
    throw new Error("fallback fetch should not be reached");
  });
}

describe("consumeSse (via askQueryStream)", () => {
  it("parses a result frame split across multiple reads", async () => {
    const payload = JSON.stringify(resultFrameData("from-stream"));
    // Split the single SSE record across read() boundaries mid-field and
    // mid-JSON to prove buffering across decode() chunks.
    fetchMock.mockResolvedValueOnce(
      sseResponse(["event: res", "ult\ndata: ", payload.slice(0, 10), payload.slice(10), "\n\n"]),
    );
    failOnFallback();

    const res = await askQueryStream("q");
    expect(res.answer).toBe("from-stream");
    // Only the streaming fetch ran; no fallback.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/query/stream");
  });

  it("tolerates CRLF line endings", async () => {
    const payload = JSON.stringify(resultFrameData("crlf"));
    fetchMock.mockResolvedValueOnce(sseResponse([`event: result\r\ndata: ${payload}\r\n\r\n`]));
    failOnFallback();

    const res = await askQueryStream("q");
    expect(res.answer).toBe("crlf");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("skips comment / keep-alive lines and emits status before the result", async () => {
    const onStatus = vi.fn();
    const payload = JSON.stringify(resultFrameData("after-status"));
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        ": keep-alive\n", // SSE comment, ignored
        `event: status\ndata: ${JSON.stringify({ text: "searching" })}\n\n`,
        ": another ping\n",
        `event: status\ndata: ${JSON.stringify({ text: "ranking" })}\n\n`,
        `event: result\ndata: ${payload}\n\n`,
      ]),
    );
    failOnFallback();

    const res = await askQueryStream("q", null, null, { onStatus });
    expect(res.answer).toBe("after-status");
    // Status callbacks fired in order, strictly before the resolved result.
    expect(onStatus.mock.calls.map((c) => c[0])).toEqual(["searching", "ranking"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("dispatches a final result record with no trailing blank line", async () => {
    const payload = JSON.stringify(resultFrameData("no-trailing-newline"));
    // event line is newline-terminated (so eventName is set in the loop); the
    // data line carries NO trailing newline, exercising the post-close drain.
    fetchMock.mockResolvedValueOnce(sseResponse([`event: result\ndata: ${payload}`]));
    failOnFallback();

    const res = await askQueryStream("q");
    expect(res.answer).toBe("no-trailing-newline");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("falls back to plain /query when the stream closes without a result frame", async () => {
    // Only status frames, no result: consumeSse returns null and the caller
    // must run the fallback POST /query.
    fetchMock.mockResolvedValueOnce(
      sseResponse([`event: status\ndata: ${JSON.stringify({ text: "working" })}\n\n`]),
    );
    // Fallback fetch returns a normal JSON /query response.
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(resultFrameData("from-fallback")), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const onStatus = vi.fn();
    const res = await askQueryStream("q", null, null, { onStatus });
    expect(res.answer).toBe("from-fallback");
    // Two fetches: the stream, then the fallback.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain("/query");
    // The page is told the stream died BEFORE the re-run, so it can discard
    // the stale provisional draft and show progress for the fallback.
    expect(onStatus).toHaveBeenLastCalledWith(STREAM_FALLBACK_STATUS);
  });

  it("falls back exactly once when the body errors mid-stream", async () => {
    // A status frame arrives, then the ReadableStream errors (network drop
    // mid-body). consumeSse throws a non-abort error and askQueryStream must
    // issue exactly one fallback POST /query.
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(enc.encode(`event: status\ndata: ${JSON.stringify({ text: "working" })}\n\n`));
        controller.error(new Error("network"));
      },
    });
    fetchMock.mockResolvedValueOnce(
      new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(resultFrameData("from-fallback")), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const onStatus = vi.fn();
    const res = await askQueryStream("q", null, null, { onStatus });
    expect(res.answer).toBe("from-fallback");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain("/query");
    expect(onStatus).toHaveBeenLastCalledWith(STREAM_FALLBACK_STATUS);
  });

  it("rethrows a caller abort mid-stream without falling back (never double-send)", async () => {
    // Real fetch errors the body stream when the request signal aborts; the
    // mock mirrors that so the mid-body AbortError path is exercised for real.
    const onStatus = vi.fn();
    fetchMock.mockImplementationOnce((_input: RequestInfo | URL, init?: RequestInit) => {
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(enc.encode(`event: status\ndata: ${JSON.stringify({ text: "working" })}\n\n`));
          init?.signal?.addEventListener(
            "abort",
            () => controller.error(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        },
      });
      return Promise.resolve(
        new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } }),
      );
    });
    failOnFallback();

    const controller = new AbortController();
    const promise = askQueryStream("q", null, null, { onStatus }, false, controller.signal);
    // Abort only after the stream is provably mid-body (first frame delivered).
    await vi.waitFor(() => expect(onStatus).toHaveBeenCalledWith("working"));
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    // The client-side single-send guarantee: no fallback fetch, no retry status.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onStatus).not.toHaveBeenCalledWith(STREAM_FALLBACK_STATUS);
  });

  it("delivers token deltas in order, then resolves with the result", async () => {
    const onToken = vi.fn();
    const payload = JSON.stringify(resultFrameData("final"));
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        `event: token\ndata: ${JSON.stringify({ delta: "A fasting " })}\n\n`,
        `event: token\ndata: ${JSON.stringify({ delta: "study [PSG_1, p.3]." })}\n\n`,
        `event: result\ndata: ${payload}\n\n`,
      ]),
    );
    failOnFallback();

    const res = await askQueryStream("q", null, null, { onToken });
    expect(res.answer).toBe("final");
    // Provisional deltas arrived in order, strictly before the resolved result.
    expect(onToken.mock.calls.map((c) => c[0])).toEqual(["A fasting ", "study [PSG_1, p.3]."]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("ignores a malformed token frame and still resolves (draft is cosmetic)", async () => {
    const onToken = vi.fn();
    const payload = JSON.stringify(resultFrameData("ok"));
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        "event: token\ndata: {not valid json}\n\n",
        `event: token\ndata: ${JSON.stringify({ delta: "real" })}\n\n`,
        `event: result\ndata: ${payload}\n\n`,
      ]),
    );
    failOnFallback();

    const res = await askQueryStream("q", null, null, { onToken });
    expect(res.answer).toBe("ok");
    // The malformed token was skipped; the valid one still fired.
    expect(onToken.mock.calls.map((c) => c[0])).toEqual(["real"]);
  });

  it("delivers status and token callbacks together", async () => {
    const onStatus = vi.fn();
    const onToken = vi.fn();
    const payload = JSON.stringify(resultFrameData("both"));
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        `event: status\ndata: ${JSON.stringify({ text: "searching" })}\n\n`,
        `event: token\ndata: ${JSON.stringify({ delta: "chunk" })}\n\n`,
        `event: result\ndata: ${payload}\n\n`,
      ]),
    );
    failOnFallback();

    const res = await askQueryStream("q", null, null, { onStatus, onToken });
    expect(res.answer).toBe("both");
    expect(onStatus).toHaveBeenCalledWith("searching");
    expect(onToken).toHaveBeenCalledWith("chunk");
  });

  it("delivers draft deltas in order, then resolves with the result", async () => {
    const onDraft = vi.fn();
    const payload = JSON.stringify(resultFrameData("final"));
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        `event: draft\ndata: ${JSON.stringify({ delta: "A fasting " })}\n\n`,
        `event: draft\ndata: ${JSON.stringify({ delta: "study [1]." })}\n\n`,
        `event: result\ndata: ${payload}\n\n`,
      ]),
    );
    failOnFallback();

    const res = await askQueryStream("q", null, null, { onDraft }, true);
    expect(res.answer).toBe("final");
    // Raw, un-gated deltas arrived in order, strictly before the result.
    expect(onDraft.mock.calls.map((c) => c[0])).toEqual(["A fasting ", "study [1]."]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("ignores a malformed draft frame and still resolves (draft is cosmetic)", async () => {
    const onDraft = vi.fn();
    const payload = JSON.stringify(resultFrameData("ok"));
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        "event: draft\ndata: {not valid json}\n\n",
        `event: draft\ndata: ${JSON.stringify({ delta: "real" })}\n\n`,
        `event: result\ndata: ${payload}\n\n`,
      ]),
    );
    failOnFallback();

    const res = await askQueryStream("q", null, null, { onDraft }, true);
    expect(res.answer).toBe("ok");
    expect(onDraft.mock.calls.map((c) => c[0])).toEqual(["real"]);
  });

  it("invokes onDraftReset on a draft_reset frame, with no payload to parse", async () => {
    const onDraft = vi.fn();
    const onDraftReset = vi.fn();
    const payload = JSON.stringify(resultFrameData("final"));
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        `event: draft\ndata: ${JSON.stringify({ delta: "partial " })}\n\n`,
        "event: draft_reset\ndata: {}\n\n",
        `event: draft\ndata: ${JSON.stringify({ delta: "restarted" })}\n\n`,
        `event: result\ndata: ${payload}\n\n`,
      ]),
    );
    failOnFallback();

    const res = await askQueryStream("q", null, null, { onDraft, onDraftReset }, true);
    expect(res.answer).toBe("final");
    expect(onDraft.mock.calls.map((c) => c[0])).toEqual(["partial ", "restarted"]);
    expect(onDraftReset).toHaveBeenCalledTimes(1);
  });

  it("ignores an unknown SSE event name and still resolves", async () => {
    const onStatus = vi.fn();
    const payload = JSON.stringify(resultFrameData("still-ok"));
    fetchMock.mockResolvedValueOnce(
      sseResponse([
        `event: mystery\ndata: ${JSON.stringify({ whatever: 1 })}\n\n`,
        `event: status\ndata: ${JSON.stringify({ text: "searching" })}\n\n`,
        `event: result\ndata: ${payload}\n\n`,
      ]),
    );
    failOnFallback();

    const res = await askQueryStream("q", null, null, { onStatus });
    expect(res.answer).toBe("still-ok");
    expect(onStatus).toHaveBeenCalledWith("searching");
  });
});

describe("askQueryStream request body -- live_draft opt-in", () => {
  it("carries live_draft: true only when the caller requests it", async () => {
    const payload = JSON.stringify(resultFrameData("a"));
    fetchMock.mockResolvedValueOnce(sseResponse([`event: result\ndata: ${payload}\n\n`]));
    failOnFallback();

    await askQueryStream("q", null, null, undefined, true);
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body).toEqual({
      question: "q",
      filters: null,
      session_id: null,
      origin: "thread",
      live_draft: true,
    });
  });

  it("omits live_draft entirely when not requested (default false)", async () => {
    const payload = JSON.stringify(resultFrameData("b"));
    fetchMock.mockResolvedValueOnce(sseResponse([`event: result\ndata: ${payload}\n\n`]));
    failOnFallback();

    await askQueryStream("q", null, null);
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body).toEqual({ question: "q", filters: null, session_id: null, origin: "thread" });
    expect(body.live_draft).toBeUndefined();
  });
});

// Issue #208: /query has exactly one place to put a conversation, so this
// field is how the Assistant panel's lookups stay out of
// ListChatSessionsForUser's thread filter. origin defaults to "thread" for
// every existing caller; a non-default value has to survive a stream retry
// too, since a missed fallback call site would silently re-file a lookup as
// a visible thread the moment its stream had to retry.
describe("askQueryStream request body -- origin", () => {
  it("defaults origin to thread and carries a non-default value onto the wire", async () => {
    const payload = JSON.stringify(resultFrameData("a"));
    fetchMock.mockResolvedValueOnce(sseResponse([`event: result\ndata: ${payload}\n\n`]));
    failOnFallback();

    await askQueryStream("q", null, null, undefined, false, undefined, "assistant");
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.origin).toBe("assistant");
  });

  it("carries a non-default origin through the pre-headers fallback to plain /query", async () => {
    // The first fetch (the stream) fails before headers arrive; askQueryStream
    // must retry with plain POST /query and forward origin onto that retry too.
    fetchMock.mockRejectedValueOnce(new TypeError("network down"));
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(resultFrameData("from-fallback")), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const res = await askQueryStream("q", null, null, undefined, false, undefined, "assistant");
    expect(res.answer).toBe("from-fallback");
    const fallbackBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(fallbackBody.origin).toBe("assistant");
  });

  it("carries a non-default origin through the no-result-frame fallback to plain /query", async () => {
    fetchMock.mockResolvedValueOnce(
      sseResponse([`event: status\ndata: ${JSON.stringify({ text: "working" })}\n\n`]),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(resultFrameData("from-fallback")), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const res = await askQueryStream("q", null, null, undefined, false, undefined, "assistant");
    expect(res.answer).toBe("from-fallback");
    const fallbackBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(fallbackBody.origin).toBe("assistant");
  });
});

// Half-open connection guard: once headers arrive the TTFB timer is disarmed,
// so a socket that goes silent without FIN/RST would otherwise pend
// reader.read() forever (composer locked, fallback unreachable). The watchdog
// cancels the reader after 75s of total silence; the server's ~15s keep-alive
// comments keep any healthy stream well inside the window.
describe("SSE inactivity watchdog", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function openStreamResponse(): {
    response: Response;
    controller: ReadableStreamDefaultController<Uint8Array>;
  } {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        controller = c;
      },
    });
    const response = new Response(stream, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
    return { response, controller };
  }

  it("cancels a silent stream after the idle window and falls back to /query", async () => {
    const { response, controller } = openStreamResponse();
    fetchMock.mockResolvedValueOnce(response);
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(resultFrameData("from-fallback")), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const onStatus = vi.fn();
    const promise = askQueryStream("q", null, null, { onStatus });
    await vi.advanceTimersByTimeAsync(0);
    controller.enqueue(enc.encode(`event: status\ndata: ${JSON.stringify({ text: "working" })}\n\n`));
    await vi.advanceTimersByTimeAsync(0);
    expect(onStatus).toHaveBeenCalledWith("working");
    // The stream then goes silent past the 75s window: the watchdog cancels
    // the reader and the transparent /query fallback takes over.
    await vi.advanceTimersByTimeAsync(76_000);
    const res = await promise;
    expect(res.answer).toBe("from-fallback");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain("/query");
    expect(onStatus).toHaveBeenLastCalledWith(STREAM_FALLBACK_STATUS);
    // No timer survives: watchdog, TTFB, and fallback bounds all released.
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps a slow stream alive while keep-alive comments arrive inside the window", async () => {
    const { response, controller } = openStreamResponse();
    fetchMock.mockResolvedValueOnce(response);
    failOnFallback();

    const payload = JSON.stringify(resultFrameData("slow-but-alive"));
    const promise = askQueryStream("q");
    await vi.advanceTimersByTimeAsync(0);
    // Traffic every 60s (< 75s window) for 120s total (> 75s): comment frames
    // must re-arm the watchdog, so the stream survives to its result.
    controller.enqueue(enc.encode(": keep-alive\n"));
    await vi.advanceTimersByTimeAsync(0); // let the read re-arm before time moves
    await vi.advanceTimersByTimeAsync(60_000);
    controller.enqueue(enc.encode(": keep-alive\n"));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(60_000);
    controller.enqueue(enc.encode(`event: result\ndata: ${payload}\n\n`));
    controller.close();
    const res = await promise;
    expect(res.answer).toBe("slow-but-alive");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
