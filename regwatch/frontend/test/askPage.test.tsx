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

type StreamCallbacks = {
  onStatus?: (text: string) => void;
  onToken?: (delta: string) => void;
  onDraft?: (delta: string) => void;
  onDraftReset?: () => void;
};

// The factories below only CALL these at runtime (after vi.mock hoists), so
// they never read them during hoist — same pattern as WatchPage.test.tsx.
const askQueryStreamMock = vi.fn<
  (
    q: string,
    filters: Record<string, string> | null,
    sessionId: string | null,
    callbacks?: StreamCallbacks,
    liveDraft?: boolean,
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

// The page reads the live refusal threshold for the confidence tooltip; mock
// the provider hook the same way as useSessions above.
vi.mock("@/components/SettingsProvider", () => ({
  useSettings: () => ({
    settings: {
      company_name: "Test Co",
      embedding_provider: "test-embed",
      llm_model: "test-llm",
      llm_provider: "test-provider",
      refusal_score_threshold: 0.3,
      retrieval_top_k: 8,
    },
    reachable: true,
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

function makeSessionDetail(id: string, title = "t"): SessionDetail {
  return {
    session: { id, title, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
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

describe("AskPage -- fallback notice + status log persist onto the settled turn (B8)", () => {
  it("keeps the fallback notice and all status frames after a MID-DRAFT fallback settles", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    act(() => {
      stream.cb?.onStatus?.("Resolving product...");
      stream.cb?.onStatus?.("Searching 1,795 documents...");
      // Draft text painted BEFORE the stream died: the only shape the notice
      // is true for -- the analyst watched this text vanish.
      stream.cb?.onToken?.("half a sent");
      stream.cb?.onStatus?.(STREAM_FALLBACK_STATUS);
    });
    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);

    expect(container.querySelector(".msg__fallback")?.textContent).toBe(
      "Connection dropped mid-draft \u2014 the answer was re-run over a fresh request and may differ from the draft.",
    );
    // statusFrames STATE is cleared in run()'s finally -- only the closure-local
    // copy stamped onto the turn can render these.
    // Children of the (closed) Provenance details are still queryable in jsdom.
    const frames = [...container.querySelectorAll(".prov__log li")].map((li) => li.textContent);
    expect(frames).toEqual([
      "Resolving product...",
      "Searching 1,795 documents...",
      STREAM_FALLBACK_STATUS,
    ]);
  });

  it("logs the retry but shows no notice when the stream fell back with no draft painted", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    // Status frames only -- not one token or draft delta ever reached the DOM.
    act(() => {
      stream.cb?.onStatus?.("Resolving product...");
      stream.cb?.onStatus?.("Searching 1,795 documents...");
      stream.cb?.onStatus?.(STREAM_FALLBACK_STATUS);
    });
    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);

    // Nothing was withdrawn, so the turn must not claim a draft was lost...
    expect(container.querySelector(".msg__fallback")).toBeNull();
    // ...while the retry itself stays on the record: the status log is
    // provenance and is NOT gated on the notice.
    const frames = [...container.querySelectorAll(".prov__log li")].map((li) => li.textContent);
    expect(frames).toEqual([
      "Resolving product...",
      "Searching 1,795 documents...",
      STREAM_FALLBACK_STATUS,
    ]);
  });

  it("shows no fallback notice when the stream 502s before the first token (#224)", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    // The exact prod shape of the 2026-08-13 outage: /query/stream answered
    // !res.ok, so api.ts emitted the fallback status with zero token/draft
    // frames behind it and the plain POST /query then answered normally. For
    // 3h35m every such turn told the analyst a draft had been dropped when
    // none had ever painted.
    act(() => stream.cb?.onStatus?.(STREAM_FALLBACK_STATUS));
    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);

    expect(container.querySelector(".msg__fallback")).toBeNull();
  });

  it("shows no fallback notice when the stream settles cleanly (control)", async () => {
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    act(() => {
      stream.cb?.onStatus?.("Resolving product...");
      stream.cb?.onStatus?.("Searching 1,795 documents...");
    });
    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);

    expect(container.querySelector(".msg__fallback")).toBeNull();
    // The status log itself is not gated on the fallback.
    expect(container.querySelectorAll(".prov__log li")).toHaveLength(2);
  });
});

describe("AskPage -- live-draft channel (withdrawal note, reset)", () => {
  it("renders the withdrawal note when the result carries draft_withdrawn", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(
      makeResponse({
        status: "refused",
        refused: true,
        citations: [],
        answer: "No matching guidance was found for this product.",
        draft_withdrawn: "refused",
      } as Partial<QueryResponse>),
    );
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    await screen.findByText("No matching guidance was found for this product.");

    const notes = [...container.querySelectorAll(".msg__fallback")].map((n) => n.textContent);
    expect(notes).toContain(
      "The provisional draft was withdrawn \u2014 it could not be verified against the cited guidance. The response below is the verified outcome.",
    );
  });

  it("says statements were dropped when draft_withdrawn is 'partial'", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(
      makeResponse({ draft_withdrawn: "partial" } as Partial<QueryResponse>),
    );
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    await screen.findByText(ANSWER_TEXT);

    expect(container.querySelector(".msg__fallback")?.textContent).toBe(
      "The provisional draft was withdrawn \u2014 some draft statements could not be verified and were dropped. The response below is the verified outcome.",
    );
  });

  it("renders no withdrawal note when draft_withdrawn is absent (control)", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(makeResponse());
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    await screen.findByText(ANSWER_TEXT);

    expect(container.querySelector(".msg__fallback")).toBeNull();
  });

  it("clears the visible draft on a mid-stream draft_reset", async () => {
    // Reduced motion for this test only: onDraft deltas land in the DOM
    // immediately (still through the shared buffer function), so the
    // assertion doesn't depend on the pacing interval's cadence.
    window.matchMedia = vi.fn().mockReturnValue({ matches: true } as MediaQueryList);
    const user = userEvent.setup();
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    act(() => stream.cb?.onDraft?.("half a sentence that will be discarded"));
    expect(container.querySelector(".msg__body--draft")?.textContent).toBe(
      "half a sentence that will be discarded",
    );

    act(() => stream.cb?.onDraftReset?.());
    expect(container.querySelector(".msg__body--draft")).toBeNull();

    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);
  });
});

describe("AskPage -- drafting milestones for the SR region (B10)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("announces the first token, then throttles Still-drafting updates to >=15s", async () => {
    // fireEvent, not user-event: user-event's internal waits deadlock against
    // a faked clock (see the note in DeficiencyPage.test.tsx). Only the
    // explicit advance moves time here, which is the determinism this needs.
    vi.useFakeTimers();
    const stream = pendingStream();
    const { container } = render(<AskPage />);
    const region = container.querySelector('.sr-only[aria-live="polite"]');

    fireEvent.change(composerBox(), { target: { value: "a question" } });
    fireEvent.keyDown(composerBox(), { key: "Enter" });
    await act(async () => {}); // flush run()'s synchronous state updates

    act(() => stream.cb?.onToken?.("first"));
    expect(region?.textContent).toBe(
      "Drafting a provisional answer \u2014 the verified answer will follow.",
    );

    act(() => {
      vi.advanceTimersByTime(15000);
      stream.cb?.onToken?.(" more");
    });
    expect(region?.textContent).toContain("Still drafting");
    const milestone = region?.textContent;

    // Inside the 15s window nothing new is announced (the region text must
    // not churn on every token).
    act(() => {
      vi.advanceTimersByTime(2000);
      stream.cb?.onToken?.(" more");
    });
    expect(region?.textContent).toBe(milestone);

    await act(async () => {
      stream.resolve?.(makeResponse());
    });
    vi.useRealTimers();
    // The settle announcement overwrites the milestone, as before.
    await screen.findByText(ANSWER_TEXT);
    await waitFor(() => expect(region?.textContent).toContain("Answer ready"));
  });
});

describe("AskPage -- count-aware clarify announcement (D19)", () => {
  it("names the option count and where to find them", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(
      makeResponse({
        answer: "Which propranolol product?",
        status: "clarify",
        refused: false,
        citations: [],
        clarify: [
          { label: "oral tablet", query: "propranolol tablet", filters: {} },
          { label: "oral solution", query: "propranolol solution", filters: {} },
        ],
      } as Partial<QueryResponse>),
    );
    const { container } = render(<AskPage />);

    await submit(user, "propranolol");
    const region = container.querySelector('.sr-only[aria-live="polite"]');
    await waitFor(() =>
      expect(region).toHaveTextContent(
        "Clarification requested \u2014 2 options to pick from, above the reply box: Which propranolol product?",
      ),
    );
  });

  it("stays plain on a legacy zero-option clarify", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(
      makeResponse({
        answer: "Which propranolol product?",
        status: "clarify",
        refused: false,
        citations: [],
        clarify: [],
      } as Partial<QueryResponse>),
    );
    const { container } = render(<AskPage />);

    await submit(user, "propranolol");
    const region = container.querySelector('.sr-only[aria-live="polite"]');
    await waitFor(() =>
      expect(region).toHaveTextContent("Clarification requested: Which propranolol product?"),
    );
    expect(region?.textContent).not.toContain("option");
  });
});

describe("AskPage -- docket header on reopened conversations (A3)", () => {
  it("renders the conversation's title and Opened date after rehydration, and clears on new chat", async () => {
    getSessionMock.mockResolvedValue(makeSessionDetail("sess-A", "Albuterol BE strategy"));
    urlSessionParam = "sess-A";
    const view = render(<AskPage />);

    await waitFor(() => expect(view.container.querySelector(".chat__docket")).not.toBeNull());
    const docket = view.container.querySelector(".chat__docket");
    expect(docket?.querySelector(".chat__docket-title")?.textContent).toBe("Albuterol BE strategy");
    expect(docket?.querySelector(".chat__docket-date")?.textContent).toMatch(/^Opened /);
    expect(screen.getByText("Conversation")).toBeInTheDocument();

    // New chat: the docket identity leaves with the thread.
    urlSessionParam = null;
    view.rerender(<AskPage />);
    await waitFor(() => expect(view.container.querySelector(".chat__docket")).toBeNull());
  });

  it("shows no docket header on a live-only conversation (the server has not named it)", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(makeResponse());
    const { container } = render(<AskPage />);

    await submit(user, "a question");
    await screen.findByText(ANSWER_TEXT);
    expect(container.querySelector(".chat__docket")).toBeNull();
  });

  it("drops the docket header when a session switch fails to load", async () => {
    getSessionMock.mockResolvedValueOnce(makeSessionDetail("sess-A", "First thread"));
    urlSessionParam = "sess-A";
    const view = render(<AskPage />);
    await waitFor(() => expect(view.container.querySelector(".chat__docket")).not.toBeNull());

    getSessionMock.mockRejectedValueOnce(new Error("boom: session unavailable"));
    urlSessionParam = "sess-B";
    view.rerender(<AskPage />);
    await screen.findByText("boom: session unavailable");
    expect(view.container.querySelector(".chat__docket")).toBeNull();
  });
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
    // findAll: the loading state now surfaces in the thread note AND the
    // composer hint (C15), so a single-element query would throw on multiple.
    await screen.findAllByText(/Opening conversation/);

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
      (_q, _f, _s, _cb, _liveDraft, signal) =>
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

    // The dropped-turn invariant, scoped to the thread: the question text now
    // legitimately reappears in the composer's restore chip (C14), so the
    // assertion is on the user bubble rather than any text on the page.
    await waitFor(() =>
      expect(view.container.querySelectorAll(".chat-row--user")).toHaveLength(0),
    );
    // The interrupted question is offered back instead of silently discarded.
    expect(screen.getByRole("button", { name: /Restore question/ })).toBeInTheDocument();
  });
});

// The composer-side polite region (programmatic scoping / pill clears /
// question recovery). The answer region keeps no role; this one is the only
// .sr-only element with role=status.
function noticeRegion(container: HTMLElement): Element | null {
  return container.querySelector('.sr-only[role="status"]');
}

describe("AskPage -- New Chat resets (restore chip, extra filters, failed sends)", () => {
  it("hands a mid-stream question back as a restore chip when New Chat cuts it off", async () => {
    const user = userEvent.setup();
    // Same abort-aware stream as the session-switch mirror above.
    askQueryStreamMock.mockImplementation(
      (_q, _f, _s, _cb, _liveDraft, signal) =>
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

    await submit(user, "question cut by new chat");
    expect(await screen.findByText("question cut by new chat")).toBeInTheDocument();

    urlSessionParam = null; // + New chat
    view.rerender(<AskPage />);

    // The optimistic turn leaves with the thread; the question is offered back.
    await waitFor(() =>
      expect(view.container.querySelectorAll(".chat-row--user")).toHaveLength(0),
    );
    expect(screen.getByRole("button", { name: /Restore question/ })).toBeInTheDocument();
    expect(screen.getByText("question cut by new chat")).toBeInTheDocument();
  });

  it("clears clarify-pick extra filters on New Chat, so the next send carries no extras", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValueOnce(
      makeResponse({
        answer: "Which propranolol product?",
        status: "clarify",
        refused: false,
        citations: [],
        clarify: [
          {
            label: "oral tablet",
            query: "propranolol tablet",
            filters: { normalized_name: "propranolol", dosage_form: "tablet", route: "oral" },
          },
        ],
      } as Partial<QueryResponse>),
    );
    askQueryStreamMock.mockResolvedValue(makeResponse());
    const view = render(<AskPage />);

    await submit(user, "propranolol");
    await user.click(await screen.findByRole("button", { name: "oral tablet" }));
    await screen.findByText(ANSWER_TEXT);
    // The pick persisted all three keys (two fields + the `route` extra).
    expect(view.container.querySelectorAll(".composer__scope-chip")).toHaveLength(3);

    // New Chat: hop onto the live session's URL first (re-entry branch), then
    // off it -- the effect only runs when the `session` param changes.
    urlSessionParam = "sess-1";
    view.rerender(<AskPage />);
    urlSessionParam = null;
    view.rerender(<AskPage />);
    await waitFor(() =>
      expect(view.container.querySelectorAll(".composer__scope-chip")).toHaveLength(0),
    );

    // The extras must not survive into the fresh conversation's first send:
    // visible fields are provably empty (above), and the wire filters are null.
    await submit(user, "an unscoped question");
    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(3));
    expect(askQueryStreamMock.mock.calls[2][1]).toBeNull();
  });

  it("routes a failed send's question into the restore chip on New Chat (never discarded silently)", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockRejectedValue(new Error("network down"));
    getSessionMock.mockResolvedValue(makeSessionDetail("sess-A"));
    urlSessionParam = "sess-A";
    const view = render(<AskPage />);
    await waitFor(() => expect(screen.queryByText(/Opening conversation/)).toBeNull());

    await submit(user, "the failed question");
    await waitFor(() => expect(view.container.querySelector(".sendfail")).not.toBeNull());

    urlSessionParam = null; // + New chat
    view.rerender(<AskPage />);

    // The client-only failed turn and its Retry notice leave with the thread,
    // but the typed question survives behind the restore chip.
    await waitFor(() => expect(view.container.querySelector(".sendfail")).toBeNull());
    expect(view.container.querySelectorAll(".chat-row--user")).toHaveLength(0);
    expect(screen.getByRole("button", { name: /Restore question/ })).toBeInTheDocument();
    expect(screen.getByText("the failed question")).toBeInTheDocument();
  });
});

describe("AskPage -- visible scope chips + one dispatch path (C11/C12)", () => {
  it("renders a chip per typed filter; clearing one omits its key from the next send", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(makeResponse());
    const { container } = render(<AskPage />);

    // Raw-cased input: the chip must state the value as it will be SENT
    // (submitQuestion lowercases normalized_name), so the label reads
    // "metformin", not "Metformin".
    fireEvent.change(screen.getByLabelText("Active ingredient filter"), {
      target: { value: "Metformin" },
    });
    fireEvent.change(screen.getByLabelText("Dosage form filter"), { target: { value: "tablet" } });
    expect(container.querySelectorAll(".composer__scope-chip")).toHaveLength(2);

    await user.click(screen.getByLabelText("Clear filter: ingredient metformin"));
    expect(screen.getByLabelText("Active ingredient filter")).toHaveValue("");
    expect(container.querySelectorAll(".composer__scope-chip")).toHaveLength(1);
    // Clearing unmounted the focused chip: focus must land in the composer,
    // not fall to <body>.
    expect(document.activeElement).toBe(composerBox());

    await submit(user, "a question");
    await screen.findByText(ANSWER_TEXT);
    expect(askQueryStreamMock.mock.calls[0][1]).toEqual({ dosage_form: "tablet" });
  });

  it("persists a clarify pick's extra filter keys into chips, the counter, and the next typed send", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValueOnce(
      makeResponse({
        answer: "Which propranolol product?",
        status: "clarify",
        refused: false,
        citations: [],
        clarify: [
          {
            label: "oral tablet",
            query: "propranolol tablet",
            filters: { normalized_name: "propranolol", dosage_form: "tablet", route: "oral" },
          },
        ],
      } as Partial<QueryResponse>),
    );
    askQueryStreamMock.mockResolvedValue(makeResponse());
    const { container } = render(<AskPage />);

    await submit(user, "propranolol");
    await user.click(await screen.findByRole("button", { name: "oral tablet" }));
    await screen.findByText(ANSWER_TEXT);

    // Two field chips + the persisted extra key, and an honest counter.
    expect(container.querySelectorAll(".composer__scope-chip")).toHaveLength(3);
    expect(screen.getByText(/3 active/)).toBeInTheDocument();
    // The programmatic scoping was announced to the composer notice region.
    expect(noticeRegion(container)?.textContent).toBe(
      "Search scoped to ingredient propranolol, form tablet, route oral.",
    );

    // Previously `route` applied to the pick and silently vanished for typed
    // follow-ups; persisting it is the semantic fix under the chips.
    await submit(user, "what strengths are covered?");
    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(3));
    expect(askQueryStreamMock.mock.calls[2][1]).toEqual({
      normalized_name: "propranolol",
      dosage_form: "tablet",
      route: "oral",
    });
  });

  it("routes starter pills through the shared dispatch: unscoped send, fields emptied, clear announced", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(makeResponse());
    const { container } = render(<AskPage />);

    fireEvent.change(screen.getByLabelText("Active ingredient filter"), {
      target: { value: "metformin" },
    });
    expect(container.querySelectorAll(".composer__scope-chip")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "propranolol" }));
    await screen.findByText(ANSWER_TEXT);

    expect(askQueryStreamMock).toHaveBeenCalledTimes(1);
    expect(askQueryStreamMock.mock.calls[0][1]).toBeNull();
    expect(screen.getByLabelText("Active ingredient filter")).toHaveValue("");
    expect(container.querySelectorAll(".composer__scope-chip")).toHaveLength(0);
    expect(noticeRegion(container)?.textContent).toBe(
      "Filters cleared \u2014 example questions run unscoped.",
    );
  });

  it("ignores pill activation while a query is in flight (no double dispatch)", async () => {
    const stream = pendingStream();
    const { container } = render(<AskPage />);

    const pill = screen.getByRole("button", { name: "propranolol" });
    // Two raw activations of the same pill node: whether React flushes between
    // them (pill unmounts with the empty state) or batches (sendStarter's busy
    // guard hits), the observable invariant is one dispatch, one inquiry row.
    fireEvent.click(pill);
    fireEvent.click(pill);
    await act(async () => {});

    expect(askQueryStreamMock).toHaveBeenCalledTimes(1);
    expect(container.querySelectorAll(".chat-row--user")).toHaveLength(1);

    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);
  });
});

describe("AskPage -- failed sends stay in-thread with Retry (C13)", () => {
  it("keeps the inquiry turn and renders the transport notice, not the composer error", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockRejectedValue(new Error("network down"));
    const { container } = render(<AskPage />);

    await submit(user, "my question");
    await waitFor(() => expect(container.querySelector(".sendfail")).not.toBeNull());

    // The optimistic turn stays: the question keeps its place in the thread.
    // (.bubble--user, not .chat-row--user: the sendfail wrapper is also a
    // right-aligned chat row, by design.)
    expect(screen.getByText("my question")).toBeInTheDocument();
    expect(container.querySelectorAll(".bubble--user")).toHaveLength(1);
    expect(container.querySelector(".composer__error")).toBeNull();
    expect(container.querySelector(".sendfail__msg")?.textContent).toBe(
      "Not sent \u2014 network down",
    );
    // Transport register, never the epistemic declined block (INV-2's home).
    expect(container.querySelector(".msg__declined")).toBeNull();
  });

  it("Retry re-fires the same question AND filters with exactly one user row; success clears the notice", async () => {
    const user = userEvent.setup();
    const { container } = render(<AskPage />);
    fireEvent.change(screen.getByLabelText("Active ingredient filter"), {
      target: { value: "metformin" },
    });
    askQueryStreamMock.mockRejectedValueOnce(new Error("boom"));
    await submit(user, "my question");
    await waitFor(() => expect(container.querySelector(".sendfail")).not.toBeNull());

    // Discriminate stored-vs-live filters: edit BOTH visible fields after the
    // failure. Retry must resend the filters STORED at failure time verbatim,
    // not whatever the fields say now.
    fireEvent.change(screen.getByLabelText("Active ingredient filter"), {
      target: { value: "aspirin" },
    });
    fireEvent.change(screen.getByLabelText("Dosage form filter"), {
      target: { value: "capsule" },
    });

    const stream = pendingStream(); // the retry call pends under our control
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(askQueryStreamMock).toHaveBeenCalledTimes(2);
    expect(askQueryStreamMock.mock.calls[1][0]).toBe("my question");
    expect(askQueryStreamMock.mock.calls[1][1]).toEqual({ normalized_name: "metformin" });
    // The Retry button unmounted with the notice; focus parks in the composer.
    expect(document.activeElement).toBe(composerBox());
    // run() re-appends the inquiry turn; the stale one was popped first.
    expect(container.querySelectorAll(".bubble--user")).toHaveLength(1);
    // The notice yields the slot back to the ticker while the retry flies.
    expect(container.querySelector(".sendfail")).toBeNull();

    act(() => stream.resolve?.(makeResponse()));
    await screen.findByText(ANSWER_TEXT);
    expect(container.querySelector(".sendfail")).toBeNull();
    expect(container.querySelectorAll(".bubble--user")).toHaveLength(1);
  });

  it("a new typed send removes the stale failed turn (it never looks server-persisted)", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockRejectedValueOnce(new Error("boom"));
    askQueryStreamMock.mockResolvedValue(makeResponse());
    const { container } = render(<AskPage />);

    await submit(user, "first question");
    await waitFor(() => expect(container.querySelector(".sendfail")).not.toBeNull());

    await submit(user, "second question");
    await screen.findByText(ANSWER_TEXT);
    expect(screen.queryByText("first question")).toBeNull();
    expect(container.querySelectorAll(".bubble--user")).toHaveLength(1);
    expect(container.querySelector(".sendfail")).toBeNull();
  });

  it("history-load failure still uses the composer error, and typing clears it", async () => {
    getSessionMock.mockRejectedValue(new Error("boom: session unavailable"));
    urlSessionParam = "sess-X";
    const { container } = render(<AskPage />);

    await screen.findByText("boom: session unavailable");
    expect(container.querySelector(".composer__error")).not.toBeNull();

    // The first edit signals "moving on" -- the alert stops shouting over it.
    fireEvent.change(composerBox(), { target: { value: "a" } });
    expect(container.querySelector(".composer__error")).toBeNull();
  });
});

describe("AskPage -- stop/session-switch hand the question back (C14)", () => {
  // Abort-aware stream: rejects like the real api layer so run() settles.
  function abortableStream() {
    askQueryStreamMock.mockImplementation(
      (_q, _f, _s, _cb, _liveDraft, signal) =>
        new Promise<QueryResponse>((_resolve, reject) => {
          signal?.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        }),
    );
  }

  it("Stop with typed text leaves the composer alone and offers a restore chip that fills only when empty", async () => {
    const user = userEvent.setup();
    abortableStream();
    const { container } = render(<AskPage />);

    await submit(user, "the cancelled question");
    fireEvent.change(composerBox(), { target: { value: "typed draft" } });
    await user.click(screen.getByLabelText("Stop generating"));

    // Typed work is never clobbered; the cancelled question parks in a chip.
    expect(composerBox()).toHaveValue("typed draft");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Restore question/ })).toBeInTheDocument(),
    );
    expect(noticeRegion(container)?.textContent).toBe(
      "Stopped \u2014 the cancelled question can be restored below.",
    );

    // Clicking with text still present refuses and says why.
    await user.click(screen.getByRole("button", { name: /Restore question/ }));
    expect(composerBox()).toHaveValue("typed draft");
    expect(noticeRegion(container)?.textContent).toBe(
      "Clear the composer first \u2014 restoring will not overwrite typed text.",
    );

    // Emptying then clicking fills, and the chip leaves.
    fireEvent.change(composerBox(), { target: { value: "" } });
    await user.click(screen.getByRole("button", { name: /Restore question/ }));
    expect(composerBox()).toHaveValue("the cancelled question");
    expect(screen.queryByRole("button", { name: /Restore question/ })).toBeNull();
  });

  it("offers no restore chip after a normal send", async () => {
    const user = userEvent.setup();
    askQueryStreamMock.mockResolvedValue(makeResponse());
    render(<AskPage />);

    await submit(user, "a question");
    await screen.findByText(ANSWER_TEXT);
    expect(screen.queryByRole("button", { name: /Restore question/ })).toBeNull();
  });

  it("re-announces a consecutive identical notice (the flushSync clear commits separately)", async () => {
    const user = userEvent.setup();
    abortableStream();
    const { container } = render(<AskPage />);

    // Park a question behind the restore chip, then leave text in the composer
    // so clicking Restore refuses with the same notice every time.
    await submit(user, "the cancelled question");
    fireEvent.change(composerBox(), { target: { value: "typed draft" } });
    await user.click(screen.getByLabelText("Stop generating"));
    await screen.findByRole("button", { name: /Restore question/ });

    const region = noticeRegion(container)!;
    await user.click(screen.getByRole("button", { name: /Restore question/ }));
    expect(region.textContent).toBe(
      "Clear the composer first \u2014 restoring will not overwrite typed text.",
    );

    // The second, identical refusal. Without the flushSync clear, the
    // clear + set batch into a no-op DOM diff (the committed text is already
    // identical) and a polite region never re-announces. The observer stands
    // in for the screen reader: it must see the region actually change.
    const seen: MutationRecord[] = [];
    const observer = new MutationObserver((records) => seen.push(...records));
    observer.observe(region, { childList: true, characterData: true, subtree: true });
    await user.click(screen.getByRole("button", { name: /Restore question/ }));
    seen.push(...observer.takeRecords());
    observer.disconnect();

    expect(seen.length).toBeGreaterThan(0);
    expect(region.textContent).toBe(
      "Clear the composer first \u2014 restoring will not overwrite typed text.",
    );
  });
});

describe("AskPage -- composer explains itself while a conversation opens (C15)", () => {
  it("shows the paused hint and placeholder while history loads, and drops both on resolve", async () => {
    let resolveSession: ((d: SessionDetail) => void) | undefined;
    getSessionMock.mockImplementation(
      () =>
        new Promise<SessionDetail>((resolve) => {
          resolveSession = resolve;
        }),
    );
    urlSessionParam = "sess-A";
    const { container } = render(<AskPage />);

    await waitFor(() => expect(container.querySelector(".composer__hint")).not.toBeNull());
    expect(container.querySelector(".composer__hint")?.textContent).toBe(
      "Opening conversation \u2014 sending is paused until it loads.",
    );
    expect(composerBox()).toHaveAttribute("placeholder", "Opening conversation\u2026");
    // Typing stays live -- only sending is gated.
    expect(composerBox()).not.toBeDisabled();
    // The load is ALSO announced through the persistent composer notice
    // region -- VoiceOver can miss the freshly-inserted role=status hint.
    expect(noticeRegion(container)?.textContent).toBe("Opening conversation");

    await act(async () => {
      resolveSession?.(makeSessionDetail("sess-A"));
    });
    await waitFor(() => expect(container.querySelector(".composer__hint")).toBeNull());
    expect(composerBox().getAttribute("placeholder")).toContain("Ask about an FDA guidance");
    // The settled load clears its notice -- it must not linger as if a
    // conversation were still opening.
    expect(noticeRegion(container)?.textContent).toBe("");
  });
});

describe("AskPage -- empty-state copy dedupe (D17)", () => {
  it("states the cited-answers contract exactly once; the empty note is instruction", () => {
    render(<AskPage />);
    expect(screen.getAllByText(/every claim cited/)).toHaveLength(1);
    expect(
      screen.getByText("Ask in your own words, or start from an example below."),
    ).toBeInTheDocument();
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
