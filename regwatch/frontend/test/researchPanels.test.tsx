// The three record panels.
//
// These cover the behaviour a reader cannot see by looking at the markup: that
// the corpus is not fetched until it is asked for, that the scope chip is a
// control and not a caption, that the assistant's conversation stays one
// conversation, and that each panel's empty state states which KIND of empty
// it is.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantPanel } from "@/components/research/AssistantPanel";
import { HistoryPanel } from "@/components/research/HistoryPanel";
import { RecordPanel } from "@/components/research/RecordPanel";
import type {
  Citation,
  PsgLibraryDoc,
  QueryOrigin,
  QueryResponse,
  StreamCallbacks,
} from "@/lib/api";
import { fileAuthorities, fileHistory, type RecordFiling } from "@/lib/research-record";
import type { Turn } from "@/lib/turns";

// The factories only CALL these at runtime (after vi.mock hoists) -- the same
// partial-mock pattern askPage.test.tsx and historyPanel.test.tsx use.
const fetchPsgLibraryMock = vi.fn<() => Promise<PsgLibraryDoc[]>>();
// Full arity, through origin, so a test can pin what this panel sends on the
// last positional argument -- the whole surface of issue #208 from the
// frontend's side.
const askQueryStreamMock =
  vi.fn<
    (
      question: string,
      filters: Record<string, string> | null,
      sessionId: string | null,
      callbacks?: StreamCallbacks,
      liveDraft?: boolean,
      signal?: AbortSignal,
      origin?: QueryOrigin,
    ) => Promise<QueryResponse>
  >();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchPsgLibrary: () => fetchPsgLibraryMock(),
    askQueryStream: (...args: Parameters<typeof askQueryStreamMock>) => askQueryStreamMock(...args),
  };
});

function citation(shortName: string, page: number, extra: Partial<Citation> = {}): Citation {
  return {
    chunk_id: `${shortName}-${page}`,
    doc_id: 1,
    page,
    short_name: shortName,
    snippet: `${shortName} p.${page} snippet`,
    source_url: `https://fda.test/${shortName}`,
    version_id: 1,
    ...extra,
  };
}

function assistant(overrides: Partial<Turn> = {}): Turn {
  return {
    role: "assistant",
    content: "an answer",
    status: "answer",
    refused: false,
    citations: [],
    clarify: [],
    related: [],
    interpretation: null,
    reason: null,
    live: false,
    meta: null,
    createdAt: "2026-08-12T14:32:00",
    id: null,
    statusLog: [],
    streamFellBack: false,
    draftWithdrawn: null,
    ...overrides,
  };
}

function ask(content: string): Turn {
  return assistant({ role: "user", content, status: null });
}

function psg(id: number, over: Partial<PsgLibraryDoc> = {}): PsgLibraryDoc {
  return {
    id,
    active_ingredient: "Albuterol Sulfate",
    appl_no: "020503",
    dosage_form: "Aerosol, Metered",
    normalized_name: "albuterol sulfate",
    psg_type: "final",
    recommended_date: "2024-11-01",
    route: "Inhalation",
    source_url: "https://fda.test/psg",
    stripped_name: "albuterol",
    ...over,
  };
}

function answer(over: Partial<QueryResponse> = {}): QueryResponse {
  return {
    answer: "The guidance recommends a single-dose crossover study.",
    citations: [citation("PSG Albuterol", 4)],
    status: "answer",
    refused: false,
    session_id: "sess-1",
    ...over,
  } as QueryResponse;
}

/** The studio scopes every panel selector, so the harness supplies the scope. */
function studio(node: React.ReactNode): React.JSX.Element {
  return <div className="rw-studio">{node}</div>;
}

const CITED: readonly Turn[] = [
  ask("what BE study is required?"),
  assistant({ citations: [citation("PSG Albuterol", 4, { psg_type: "final" })] }),
];

function filingsOf(turns: readonly Turn[]): readonly RecordFiling[] {
  return fileAuthorities(turns);
}

beforeEach(() => {
  fetchPsgLibraryMock.mockReset();
  askQueryStreamMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RecordPanel", () => {
  // The panel is 24rem of provenance the analyst asked for. Paying for a
  // 1,795-row catalog to serve it would be the wrong trade, and a fetch on
  // mount is invisible until somebody profiles it.
  it("does not touch the corpus until it is searched", () => {
    render(
      studio(
        <RecordPanel kind="thread" filings={filingsOf(CITED)} onJump={vi.fn()} onClose={vi.fn()} />,
      ),
    );
    expect(fetchPsgLibraryMock).not.toHaveBeenCalled();
    expect(
      screen.getByText(/loads on your first search and is filtered here after that/i),
    ).toBeInTheDocument();
  });

  it("files each source under its question and points back at the turn", async () => {
    const onJump = vi.fn();
    const filings = filingsOf(CITED);
    render(
      studio(<RecordPanel kind="thread" filings={filings} onJump={onJump} onClose={vi.fn()} />),
    );

    expect(screen.getByText("PSG Albuterol")).toBeInTheDocument();
    expect(screen.getByText("p.4 · rev not recorded")).toBeInTheDocument();
    expect(screen.getByText("Final")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: /what BE study is required\?/i }),
    );
    expect(onJump).toHaveBeenCalledWith(filings[0].key);
  });

  // "You have not asked anything yet" and "this kind does not file here" are
  // different sentences, and only one of them is about the analyst's work.
  it("says which kind of empty it is", () => {
    const { rerender } = render(
      studio(<RecordPanel kind="thread" filings={[]} onJump={vi.fn()} onClose={vi.fn()} />),
    );
    expect(screen.getByText("Nothing filed yet")).toBeInTheDocument();

    rerender(
      studio(<RecordPanel kind="dossier" filings={[]} onJump={vi.fn()} onClose={vi.fn()} />),
    );
    expect(screen.getByText("Not filed here yet")).toBeInTheDocument();
    expect(screen.getByText(/composes its sources at request time/i)).toBeInTheDocument();
  });

  it("loads the catalog once, then filters locally", async () => {
    fetchPsgLibraryMock.mockResolvedValue([
      psg(1),
      psg(2, { dosage_form: "Solution", psg_type: "draft" }),
      psg(3, {
        active_ingredient: "Metformin Hydrochloride",
        normalized_name: "metformin hydrochloride",
        stripped_name: "metformin",
        appl_no: "020357",
      }),
    ]);
    render(studio(<RecordPanel kind="thread" filings={[]} onJump={vi.fn()} onClose={vi.fn()} />));

    const field = screen.getByRole("searchbox", { name: /search the guidance corpus/i });
    await userEvent.type(field, "albuterol{Enter}");

    await waitFor(() => expect(screen.getByText(/2 matches for "albuterol"/)).toBeInTheDocument());
    expect(fetchPsgLibraryMock).toHaveBeenCalledTimes(1);

    // Narrowing further must not go back to the network.
    await userEvent.type(field, " solution");
    await waitFor(() =>
      expect(screen.getByText(/1 match for "albuterol solution"/)).toBeInTheDocument(),
    );
    expect(fetchPsgLibraryMock).toHaveBeenCalledTimes(1);
  });

  // A failed request rendered as an empty result tells the analyst something
  // false and they stop looking. Same rule the work rail holds.
  it("says the corpus is unreachable rather than showing no matches", async () => {
    fetchPsgLibraryMock.mockRejectedValue(new Error("upstream unavailable"));
    render(studio(<RecordPanel kind="thread" filings={[]} onJump={vi.fn()} onClose={vi.fn()} />));

    await userEvent.type(
      screen.getByRole("searchbox", { name: /search the guidance corpus/i }),
      "albuterol{Enter}",
    );

    await waitFor(() =>
      expect(screen.getByText(/could not reach the corpus/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/no guidance matches/i)).not.toBeInTheDocument();

    fetchPsgLibraryMock.mockResolvedValue([psg(1)]);
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    await waitFor(() => expect(screen.getByText(/1 match for/)).toBeInTheDocument());
  });
});

describe("HistoryPanel", () => {
  it("keeps a refusal in the trail and gives it no source count", async () => {
    const onJump = vi.fn();
    const turns = [
      ask("what BE study is required?"),
      assistant({ citations: [citation("PSG A", 1), citation("PSG B", 2)] }),
      ask("and for the nasal spray?"),
      assistant({ status: "refused", refused: true, reason: "low_top_score" }),
    ];
    const entries = fileHistory(turns);
    render(studio(<HistoryPanel kind="thread" entries={entries} onJump={onJump} onClose={vi.fn()} />));

    expect(screen.getByText("Answered")).toBeInTheDocument();
    expect(screen.getByText("2 sources")).toBeInTheDocument();

    expect(screen.getByText("Evidence gap")).toBeInTheDocument();
    // The declined row must not carry a count of any kind, including zero.
    expect(screen.queryByText("0 sources")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /and for the nasal spray\?/i }));
    expect(onJump).toHaveBeenCalledWith(entries[1].key);
  });

  it("renders an unnamed model as nothing rather than an empty provenance line", () => {
    const entries = fileHistory([
      ask("q"),
      assistant({ meta: { model_name: "", audit_id: 41, turn_id: "t1" } }),
    ]);
    render(studio(<HistoryPanel kind="thread" entries={entries} onJump={vi.fn()} onClose={vi.fn()} />));
    expect(screen.getByText("#41")).toBeInTheDocument();
  });

  it("says a dossier keeps no trail rather than that nothing has happened", () => {
    render(studio(<HistoryPanel kind="dossier" entries={[]} onJump={vi.fn()} onClose={vi.fn()} />));
    expect(screen.getByText("Not kept yet")).toBeInTheDocument();
  });
});

describe("AssistantPanel", () => {
  const SCOPE = { normalizedName: "albuterol sulfate", dosageForm: "Aerosol, Metered" };

  function mount(scope: typeof SCOPE | null = SCOPE): void {
    render(
      studio(
        <AssistantPanel
          kindLabel="Threads"
          title="Albuterol sulfate"
          scope={scope}
          sourceCount={2}
          questionCount={1}
          onClose={vi.fn()}
        />,
      ),
    );
  }

  it("states what it is holding, item by item", () => {
    mount();
    expect(screen.getByText("Threads · Albuterol sulfate")).toBeInTheDocument();
    expect(screen.getByText("albuterol sulfate · Aerosol, Metered")).toBeInTheDocument();
    expect(screen.getByText("2 sources across 1 question")).toBeInTheDocument();
  });

  it("says the product is unidentified rather than leaving the row out", () => {
    mount(null);
    expect(screen.getByText(/not identified yet/i)).toBeInTheDocument();
  });

  it("sends the scope it is showing", async () => {
    askQueryStreamMock.mockResolvedValue(answer());
    mount();
    await userEvent.type(
      screen.getByRole("textbox", { name: /ask the assistant/i }),
      "what BE study?",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(1));
    expect(askQueryStreamMock.mock.calls[0][1]).toEqual({
      normalized_name: "albuterol sulfate",
      dosage_form: "Aerosol, Metered",
    });
  });

  // The chip is a control, not a caption: switching it off has to reach the
  // request, or the manifest is describing something that is not happening.
  it("drops the scope from the request when the chip is switched off", async () => {
    askQueryStreamMock.mockResolvedValue(answer());
    mount();
    await userEvent.click(
      screen.getByRole("button", { name: /stop narrowing to albuterol sulfate/i }),
    );
    await userEvent.type(screen.getByRole("textbox", { name: /ask the assistant/i }), "what is this");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(1));
    expect(askQueryStreamMock.mock.calls[0][1]).toBeNull();
  });

  // A null session id creates a new row server-side, so a panel that always
  // sent null would file a fresh thread for every question anybody asked.
  it("keeps one conversation instead of filing a thread per question", async () => {
    askQueryStreamMock.mockResolvedValue(answer({ session_id: "sess-7" }));
    mount();
    const box = screen.getByRole("textbox", { name: /ask the assistant/i });

    await userEvent.type(box, "first");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(1));

    await userEvent.type(box, "second");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(2));

    expect(askQueryStreamMock.mock.calls[0][2]).toBeNull();
    expect(askQueryStreamMock.mock.calls[1][2]).toBe("sess-7");
  });

  // Issue #208: /query has one place to put a conversation, so this panel's
  // session must be marked "assistant" or it files as a visible thread
  // beside the analyst's own work in the work rail.
  it("marks its session origin so it never files a thread", async () => {
    askQueryStreamMock.mockResolvedValue(answer());
    mount();
    await userEvent.type(screen.getByRole("textbox", { name: /ask the assistant/i }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(askQueryStreamMock).toHaveBeenCalledTimes(1));
    expect(askQueryStreamMock.mock.calls[0][6]).toBe("assistant");
  });

  it("shows the sources an answer stands on", async () => {
    askQueryStreamMock.mockResolvedValue(answer());
    mount();
    await userEvent.type(screen.getByRole("textbox", { name: /ask the assistant/i }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(screen.getByText(/single-dose crossover/i)).toBeInTheDocument());
    const cite = screen.getByRole("listitem");
    expect(within(cite).getByText("p.4")).toBeInTheDocument();
  });

  // INV-2: a turn that declined renders no citation surface, and the panel must
  // not offer the "not drawn from the record" line as a substitute verdict on
  // a refusal that never claimed anything.
  it("marks a decline and hangs no sources on it", async () => {
    askQueryStreamMock.mockResolvedValue(
      answer({
        answer: "No passage scored high enough.",
        status: "refused",
        refused: true,
        reason: "low_top_score",
        citations: [citation("PSG A", 1)],
      }),
    );
    mount();
    await userEvent.type(screen.getByRole("textbox", { name: /ask the assistant/i }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(screen.getByText("Evidence gap")).toBeInTheDocument());
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(screen.queryByText(/not drawn from the record/i)).not.toBeInTheDocument();
  });

  // THE INV-2 BOUNDARY, DRAWN IN THE RIGHT PLACE. nonAnswerLabel returns null
  // for both "clarify" and "meta", so an absent decline label does NOT mean the
  // turn answered. Reading it that way put "Not drawn from the record" under a
  // clarifying question -- a verdict on grounding for a turn that made no claim
  // and owed none. Both statuses are asserted because they reach it by the same
  // route and a fix for one alone would leave the other.
  it.each(["clarify", "meta"] as const)(
    "says nothing about grounding on a %s turn, which owed none",
    async (status) => {
      askQueryStreamMock.mockResolvedValue(
        answer({ answer: "Which dosage form?", status, citations: [] }),
      );
      mount();
      await userEvent.type(screen.getByRole("textbox", { name: /ask the assistant/i }), "q");
      await userEvent.click(screen.getByRole("button", { name: "Send" }));

      await waitFor(() => expect(screen.getByText("Which dosage form?")).toBeInTheDocument());
      expect(screen.queryByText(/not drawn from the record/i)).not.toBeInTheDocument();
      expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    },
  );

  it("still states the verdict when an ANSWER carried no sources", async () => {
    askQueryStreamMock.mockResolvedValue(answer({ citations: [] }));
    mount();
    await userEvent.type(screen.getByRole("textbox", { name: /ask the assistant/i }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(screen.getByText(/not drawn from the record/i)).toBeInTheDocument(),
    );
  });

  // The reply lands in plain state, which a screen reader has no reason to
  // revisit. Without this the wait simply never ends for a non-sighted analyst.
  it("announces the outcome when a reply settles", async () => {
    askQueryStreamMock.mockResolvedValue(answer());
    mount();
    const live = screen.getByRole("status");
    expect(live).toHaveTextContent("");

    await userEvent.type(screen.getByRole("textbox", { name: /ask the assistant/i }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    // textContent, not toHaveTextContent: the latter is a SUBSTRING match, so a
    // region announcing extra words either side of the sentence would still
    // pass and the assertion would be pinning nothing.
    await waitFor(() => expect(live.textContent).toBe("Answer ready."));
  });

  it("announces a decline in the decline's own words", async () => {
    askQueryStreamMock.mockResolvedValue(
      answer({ status: "refused", refused: true, reason: "low_top_score", citations: [] }),
    );
    mount();
    await userEvent.type(screen.getByRole("textbox", { name: /ask the assistant/i }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toBe("Evidence gap - see the reply."),
    );
  });

  // A send that never reached the server must not leave a question sitting in
  // the panel as though it had been asked.
  it("hands the words back when the send fails", async () => {
    askQueryStreamMock.mockRejectedValue(new Error("upstream unavailable"));
    mount();
    const box = screen.getByRole("textbox", { name: /ask the assistant/i });
    await userEvent.type(box, "what BE study?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(screen.getByText(/not sent/i)).toBeInTheDocument());
    expect(box).toHaveValue("what BE study?");
    expect(screen.queryByText("You")).not.toBeInTheDocument();
  });
});
