// Ask page orchestration tests: the run()/stop() state machine (busy gate,
// supersede guard, atomic draft->turn swap), session-identity resets, and the
// stream-fallback draft discard. These are the client half of the "a failed or
// stopped stream must never produce a double answer" invariant, previously
// untested. The ProductScopeBar live-region case lives here too — this is the
// page-level suite for the Ask shell.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { QueryResponse, SessionDetail } from "@/lib/api";

type StreamCallbacks = { onStatus?: (text: string) => void; onToken?: (delta: string) => void };

// The factories below only CALL these at runtime (after vi.mock hoists), so
// they never read them during hoist — same pattern as WatchPage.test.tsx.
const askQueryStreamMock = vi.fn<
  (
    q: string,
    filters: Record<string, string> | null,
    sessionId: string | null,
    callbacks?: StreamCallbacks,
    signal?: AbortSignal,
  ) => Promise<QueryResponse>
>();
const getSessionMock = vi.fn<(id: string) => Promise<SessionDetail>>();

// Keep everything else real — the page imports STREAM_FALLBACK_STATUS from
// this module and the test must compare against the same constant.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    askQueryStream: (...args: Parameters<typeof askQueryStreamMock>) => askQueryStreamMock(...args),
    getSession: (id: string) => getSessionMock(id),
  };
});

// URL `session` param under test control; rerendering re-reads it, which is
// how a sidebar click / browser back-forward reaches the URL-sync effect.
let urlSessionParam: string | null = null;
const routerReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace }),
  useSearchParams: () => new URLSearchParams(urlSessionParam ? { session: urlSessionParam } : {}),
}));

const refreshSessionsMock = vi.fn(async () => {});
const setActiveSessionIdMock = vi.fn();
vi.mock("@/components/SessionsProvider", () => ({
  useSessions: () => ({
    sessions: [],
    loaded: true,
    activeSessionId: null,
    setActiveSessionId: setActiveSessionIdMock,
    refresh: refreshSessionsMock,
  }),
}));

// ProductScopeBar consumes useCurrentProduct, which throws without a provider.
vi.mock("@/components/CurrentProductProvider", () => ({
  useCurrentProduct: () => ({
    referenceProductName: "",
    applicationNumber: "",
    hasProduct: false,
    setProduct: vi.fn(),
    clearProduct: vi.fn(),
    productParams: "",
  }),
}));

import AskPage from "@/app/(shell)/page";
import { ProductScopeBar } from "@/components/ProductScopeBar";
import { STREAM_FALLBACK_STATUS } from "@/lib/api";

const ANSWER_TEXT = "The PSG recommends a fasting study design.";

function makeResponse(overrides: Partial<QueryResponse> = {}): QueryResponse {
  return {
    answer: ANSWER_TEXT,
    status: "answer",
    refused: false,
    citations: [
      {
        short_name: "PSG_020503",
        page: 3,
        snippet: "a fasting study",
        source_url: "https://www.fda.test/psg.pdf",
        score: 0.61,
      },
    ],
    clarify: [],
    related: [],
    interpretation: null,
    reason: null,
    model_name: "test-model",
    audit_id: 11,
    turn_id: "turn-1",
    session_id: "sess-1",
    ...overrides,
  } as unknown as QueryResponse;
}

function makeSessionDetail(id: string): SessionDetail {
  return {
    session: { id, title: "t", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
    messages: [],
  };
}

// A stream the test resolves by hand, with its callbacks captured so the test
// can inject status / token frames mid-flight.
function pendingStream() {
  const captured: { cb?: StreamCallbacks; resolve?: (r: QueryResponse) => void } = {};
  askQueryStreamMock.mockImplementation((_q, _f, _s, callbacks) => {
    captured.cb = callbacks;
    return new Promise<QueryResponse>((resolve) => {
      captured.resolve = resolve;
    });
  });
  return captured;
}

function composerBox(): HTMLTextAreaElement {
  return screen.getByLabelText("Ask the guidance corpus") as HTMLTextAreaElement;
}

async function submit(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(composerBox(), text);
  await user.keyboard("{Enter}");
}

const scrollIntoViewMock = vi.fn();

beforeEach(() => {
  urlSessionParam = null;
  // jsdom implements neither of these; the scroll effect calls both.
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockReturnValue({ matches: false } as MediaQueryList),
  });
  Element.prototype.scrollIntoView = scrollIntoViewMock;
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("AskPage — run()/stop() orchestration", () => {
  it("streams tokens into a provisional draft, then swaps in exactly one validated turn", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "What BE study design is recommended?");
    act(() => {
      stream.cb?.onToken?.("A provisional ");
      stream.cb?.onToken?.("draft");
    });
    expect(container.querySelector(".msg__body--draft")?.textContent).toBe("A provisional draft");

    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);

    // Atomic swap: the draft slot is gone and exactly one assistant turn exists.
    expect(container.querySelector(".msg__body--draft")).toBeNull();
    expect(container.querySelectorAll(".chat-row--user")).toHaveLength(1);
    expect(container.querySelectorAll(".chat-row")).toHaveLength(2); // user + assistant
  });

  it("ignores a second submit while a query is in flight (the busy gate)", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "first question");
    expect(askQueryStreamMock).toHaveBeenCalledTimes(1);

    await submit(user, "second question");
    expect(askQueryStreamMock).toHaveBeenCalledTimes(1); // no-op, not a double send
    expect(container.querySelectorAll(".chat-row--user")).toHaveLength(1);

    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);
  });

  it("Stop mid-stream restores the composer and appends no assistant turn even when the stale run resolves late", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "my question");
    await user.click(screen.getByLabelText("Stop generating"));

    // The optimistic inquiry turn is rolled back and the question handed back.
    expect(container.querySelector(".chat-row--user")).toBeNull();
    expect(composerBox()).toHaveValue("my question");

    // The aborted run's promise resolving late must NOT append its answer.
    act(() => stream.resolve?.(makeResponse()));
    await waitFor(() => expect(screen.queryByLabelText("Stop generating")).toBeNull());
    expect(screen.queryByText(ANSWER_TEXT)).toBeNull();
    expect(container.querySelectorAll(".chat-row")).toHaveLength(0);
  });

  it("discards the dead stream's draft when the api layer signals the /query fallback (#28)", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    act(() => stream.cb?.onToken?.("half a sent"));
    expect(container.querySelector(".msg__body--draft")).not.toBeNull();

    // The exact status api.ts emits before every fallback POST /query: the
    // frozen draft must vanish and the ticker (with the retry line) return.
    act(() => stream.cb?.onStatus?.(STREAM_FALLBACK_STATUS));
    expect(container.querySelector(".msg__body--draft")).toBeNull();
    expect(screen.getByText(STREAM_FALLBACK_STATUS)).toBeInTheDocument();

    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);
  });

  it("keeps auto-scrolling while the draft grows token-by-token (#32)", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    render(<AskPage />);

    await submit(user, "a question");
    const before = scrollIntoViewMock.mock.calls.length;
    act(() => stream.cb?.onToken?.("a token"));
    expect(scrollIntoViewMock.mock.calls.length).toBeGreaterThan(before);

    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);
  });

  it.each([
    ["refused", "low_top_score", "Evidence gap — see the reply"],
    ["error", "provider_error", "Answer unavailable — see the reply"],
    ["scope_warning", "scope_warning", "Out of scope — see the reply"],
  ] as const)(
    "announces a %s result with its specific outcome",
    async (status, reason, expectedLabel) => {
      const user = userEvent.setup();
      askQueryStreamMock.mockResolvedValue(
        makeResponse({
          answer: "Here is a safer next step.",
          status,
          refused: true,
          citations: [],
          reason,
        } as Partial<QueryResponse>),
      );
      const { container } = render(<AskPage />);

      await submit(user, "help me with this question");
      const liveRegion = container.querySelector('.sr-only[aria-live="polite"]');
      await waitFor(() =>
        expect(liveRegion).toHaveTextContent(`${expectedLabel}: Here is a safer next step.`),
      );
    },
  );
});

describe("AskPage — session identity across switches", () => {
  it("resets the session identity when a history load fails, so the next send opens a fresh session (#17)", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(makeResponse({ session_id: "sess-A" } as Partial<QueryResponse>));
    const view = render(<AskPage />);

    // Establish a live session A.
    await submit(user, "first question");
    await screen.findByText(ANSWER_TEXT);

    // Switch to session B; its history load fails.
    getSessionMock.mockRejectedValue(new Error("boom: session unavailable"));
    urlSessionParam = "sess-B";
    view.rerender(<AskPage />);
    await screen.findByText("boom: session unavailable");

    // The next send must NOT be written into session A (nor thread its memory).
    await submit(user, "a question typed over the error");
    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(2));
    expect(askQueryStreamMock.mock.calls[1][2]).toBeNull();
  });

  it("closes the evidence drawer when the URL switches to another session (#33)", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(makeResponse());
    const view = render(<AskPage />);

    await submit(user, "a question");
    await screen.findByText(ANSWER_TEXT);
    await user.click(screen.getByRole("button", { name: /PSG_020503/ }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Browser back/forward changes ?session without a click on the scrim.
    getSessionMock.mockResolvedValue(makeSessionDetail("sess-2"));
    urlSessionParam = "sess-2";
    view.rerender(<AskPage />);

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("clears the filter fields on new chat, so the last conversation's scope cannot leak (#35)", async () => {
    getSessionMock.mockResolvedValue(makeSessionDetail("sess-A"));
    urlSessionParam = "sess-A";
    const view = render(<AskPage />);
    await waitFor(() => expect(getSessionMock).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText(/Opening conversation/)).toBeNull());

    const ingredient = screen.getByLabelText("Active ingredient filter");
    const dosage = screen.getByLabelText("Dosage form filter");
    fireEvent.change(ingredient, { target: { value: "metformin hydrochloride" } });
    fireEvent.change(dosage, { target: { value: "tablet (extended release)" } });
    expect(ingredient).toHaveValue("metformin hydrochloride");

    urlSessionParam = null; // New Chat
    view.rerender(<AskPage />);

    await waitFor(() => expect(ingredient).toHaveValue(""));
    expect(dosage).toHaveValue("");
  });

  it("clears the history-loading gate when the URL returns to the session already open", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(
      makeResponse({ session_id: "sess-A" } as Partial<QueryResponse>),
    );
    getSessionMock.mockResolvedValue(makeSessionDetail("sess-A"));
    urlSessionParam = "sess-A";
    const view = render(<AskPage />);
    await waitFor(() => expect(screen.queryByText(/Opening conversation/)).toBeNull());

    // Switch to B, whose history never resolves, then come straight back to A.
    // The effect cleanup cancels B's fetch, so its guarded .finally never fires
    // and only the re-entry branch can clear the flag.
    getSessionMock.mockImplementation((id) =>
      id === "sess-B"
        ? new Promise<SessionDetail>(() => {})
        : Promise.resolve(makeSessionDetail(id)),
    );
    urlSessionParam = "sess-B";
    view.rerender(<AskPage />);
    await screen.findByText(/Opening conversation/);

    urlSessionParam = "sess-A"; // re-entry: sessionIdRef still holds A
    view.rerender(<AskPage />);

    // Without the reset, busy stays true forever: the note stays on screen, the
    // send button is disabled, run() returns at its busy guard, and no Stop
    // button renders to escape it. A reload is the only way out.
    await waitFor(() => expect(screen.queryByText(/Opening conversation/)).toBeNull());
    await submit(user, "a follow-up");
    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(1));
    expect(askQueryStreamMock.mock.calls[0][2]).toBe("sess-A");
  });

  it("drops the optimistic inquiry turn when a session switch aborts the query", async () => {
    const user = userEvent.setup();
    // Realistic: the abort rejects the in-flight stream, which makes run()'s catch
    // return early WITHOUT undoing the optimistic turn -- only stop() does that.
    askQueryStreamMock.mockImplementation(
      (_q, _f, _s, _cb, signal) =>
        new Promise<QueryResponse>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {
            once: true,
          });
        }),
    );
    getSessionMock.mockResolvedValue(makeSessionDetail("sess-A"));
    urlSessionParam = "sess-A";
    const view = render(<AskPage />);
    await waitFor(() => expect(screen.queryByText(/Opening conversation/)).toBeNull());

    await submit(user, "what dissolution method applies");
    expect(await screen.findByText("what dissolution method applies")).toBeInTheDocument();

    // Away mid-query and straight back. The re-entry branch keeps the in-memory
    // thread, so a dangling question would still be sitting there unanswered --
    // while the server, which does not abandon dispatched work, has already
    // persisted both it and its answer. The transcript would disagree with the
    // audit ledger until a hard reload.
    getSessionMock.mockImplementation((id) =>
      id === "sess-B"
        ? new Promise<SessionDetail>(() => {})
        : Promise.resolve(makeSessionDetail(id)),
    );
    urlSessionParam = "sess-B";
    view.rerender(<AskPage />);
    urlSessionParam = "sess-A";
    view.rerender(<AskPage />);

    await waitFor(() =>
      expect(screen.queryByText("what dissolution method applies")).toBeNull(),
    );
  });
});

describe("ProductScopeBar — live region scoped to the summary state (#36)", () => {
  it("drops role=status/aria-live while the picker form is open, so typing is not announced over", async () => {
    const user = userEvent.setup();
    const { container } = render(<ProductScopeBar />);
    const bar = container.querySelector(".scopebar")!;

    // Summary (empty) state: a polite status region.
    expect(bar).toHaveAttribute("role", "status");
    expect(bar).toHaveAttribute("aria-live", "polite");

    await user.click(screen.getByText("pin one here"));

    // Editing state: the form must NOT live inside a live region.
    expect(screen.getByLabelText("Reference product name")).toBeInTheDocument();
    expect(bar).not.toHaveAttribute("role");
    expect(bar).not.toHaveAttribute("aria-live");
  });
});
