import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, listSessions } from "@/lib/api";

// handle() is module-private; listSessions() is a plain GET that funnels through
// it, so it is the cheapest public driver. What is under test is what an analyst
// actually reads when a hop BETWEEN the browser and the API answers instead of the
// API itself: the Vercel router replies text/plain, the Go proxy replies
// "upstream unavailable", and a captive portal can reply HTML with a 200. Before
// this, every one of those produced either the debug string "GET /sessions -> 502"
// or a raw V8 parser message.

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// listSessions() must reject; hand the error back for assertions rather than
// letting a resolve silently pass the test.
async function rejection(): Promise<ApiError> {
  try {
    await listSessions();
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("listSessions resolved; expected it to reject");
}

describe("handle() - non-JSON error bodies", () => {
  it("adopts a short text/plain gateway body instead of the debug status string", async () => {
    // Byte-for-byte the shape prod returns for an over-ceiling upload.
    const body =
      "An error occurred with this application.\n\n" +
      "ROUTER_EXTERNAL_TARGET_CONNECTION_ERROR_CD8\n\niad1::dc65c-1785852317219";
    fetchMock.mockResolvedValue(
      new Response(body, { status: 502, headers: { "content-type": "text/plain" } }),
    );

    const err = await rejection();
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
    // The one diagnostic anyone gets: ApiError never reaches Sentry.
    expect(err.message).toContain("ROUTER_EXTERNAL_TARGET_CONNECTION_ERROR_CD8");
    // Regressing to the old behavior puts the route back in the message.
    expect(err.message).not.toContain("/sessions");
  });

  it("adopts the Go proxy's plain-text 502", async () => {
    fetchMock.mockResolvedValue(
      new Response("upstream unavailable\n", {
        status: 502,
        headers: { "content-type": "text/plain" },
      }),
    );

    const err = await rejection();
    expect(err.status).toBe(502);
    expect(err.message).toBe("upstream unavailable");
  });

  it("refuses an HTML error page and falls back to the synthetic string", async () => {
    // A full gateway HTML document would render as garbage in the many UI sites
    // that print err.message verbatim, so the shape guard must reject it.
    const html = `<!DOCTYPE html><html><head><title>502</title></head><body>${"x".repeat(400)}</body></html>`;
    fetchMock.mockResolvedValue(
      new Response(html, { status: 502, headers: { "content-type": "text/html" } }),
    );

    const err = await rejection();
    expect(err.status).toBe(502);
    expect(err.message).not.toContain("<");
    expect(err.message).not.toContain("DOCTYPE");
    expect(err.message).toContain("502");
  });

  it("keeps a real backend detail verbatim on 422 (the whitepaper resolution contract)", async () => {
    // whitepaper/page.tsx renders er.detail as what the backend actually FOUND.
    // Client-authored prose in that slot would be an unsupported claim (INV-1).
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "No application 999999 found." }), {
        status: 422,
        headers: { "content-type": "application/json" },
      }),
    );

    const err = await rejection();
    expect(err.status).toBe(422);
    expect(err.detail).toBe("No application 999999 found.");
  });

  it("keeps a real backend detail on 5xx rather than replacing it with generic copy", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "analysis worker crashed" }), {
        status: 500,
        headers: { "content-type": "application/json" },
      }),
    );

    const err = await rejection();
    expect(err.status).toBe(500);
    expect(err.detail).toBe("analysis worker crashed");
  });

  it("still unpacks the FastAPI 422 array form", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ detail: [{ loc: ["body", "question"], msg: "field required", type: "x" }] }),
        { status: 422, headers: { "content-type": "application/json" } },
      ),
    );

    const err = await rejection();
    expect(err.detail).toBe("field required");
  });
});

describe("handle() - non-JSON success bodies", () => {
  it("rejects a 200 that is not JSON instead of leaking a parser message", async () => {
    fetchMock.mockResolvedValue(
      new Response("<!DOCTYPE html><html><body>Sign in to the network</body></html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      }),
    );

    const err = await rejection();
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
    // The old behavior surfaced V8's "Unexpected token '<', "<!DOCTYPE "...".
    expect(err.message).not.toContain("Unexpected token");
  });

  it("rejects truncated JSON on a 200 the same way", async () => {
    fetchMock.mockResolvedValue(
      new Response('{"sessions": [', {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const err = await rejection();
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
  });
});
