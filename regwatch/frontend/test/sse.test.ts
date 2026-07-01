import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { askQueryStream } from "@/lib/api";
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

    const res = await askQueryStream("q");
    expect(res.answer).toBe("from-fallback");
    // Two fetches: the stream, then the fallback.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain("/query");
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
});
