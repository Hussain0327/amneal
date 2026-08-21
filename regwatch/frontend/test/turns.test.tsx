import { fireEvent, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Intercept only the feedback POST; everything else on @/lib/api stays real
// (same partial-mock pattern as askPage.test.tsx).
const sendFeedbackMock = vi.fn<(auditId: number, rating: 1 | -1, comment: string | null) => Promise<void>>(
  async () => {},
);
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    sendFeedback: (...args: Parameters<typeof sendFeedbackMock>) => sendFeedbackMock(...args),
  };
});

import { AssistantTurn, ProvisionalDraft, UserTurn } from "@/components/Turns";
import type { ChatMessage, Citation } from "@/lib/api";
import { formatClock, formatFiled, parseApiDate } from "@/lib/time";
import { confidenceTitle, nonAnswerLabel, reasonCopy, turnFromMessage, userTurn, type Turn } from "@/lib/turns";

// A rehydrated history message carries only the persisted fields; the rest
// default inside turnFromMessage. Cast keeps the test independent of the
// generated wire shape.
const historyMessage = {
  id: "m1",
  turn_id: "t1",
  role: "assistant",
  content: "An answer.",
  status: "answer",
  citations: [],
  created_at: "2026-01-01T00:00:00Z",
} as unknown as ChatMessage;

describe("rise gate — the live flag", () => {
  it("marks a freshly-sent turn live (eligible for the reveal)", () => {
    expect(userTurn("hello").live).toBe(true);
  });

  it("marks a rehydrated history turn NOT live, so a reopened chat opens static", () => {
    expect(turnFromMessage(historyMessage).live).toBe(false);
  });

  it("keeps the server's created_at and row id on a rehydrated turn", () => {
    const turn = turnFromMessage(historyMessage);
    expect(turn.createdAt).toBe("2026-01-01T00:00:00Z");
    expect(turn.id).toBe("m1");
  });

  it("stamps a live turn with parseable client time and no server id", () => {
    const turn = userTurn("hello");
    expect(turn.id).toBeNull();
    expect(turn.createdAt).not.toBeNull();
    expect(parseApiDate(turn.createdAt ?? "")).not.toBeNull();
  });

  it("rehydrates with an empty stream trace (history persists no frames)", () => {
    const turn = turnFromMessage(historyMessage);
    expect(turn.statusLog).toEqual([]);
    expect(turn.streamFellBack).toBe(false);
  });

  it("applies the .rise class only when the turn is live", () => {
    const { container, rerender } = render(<UserTurn content="hi" live />);
    expect(container.querySelector(".rise")).not.toBeNull();
    rerender(<UserTurn content="hi" live={false} />);
    expect(container.querySelector(".rise")).toBeNull();
  });
});

describe("reasonCopy — plain-language decline/clarify reasons", () => {
  it("maps a known backend reason code to analyst copy", () => {
    expect(reasonCopy("low_top_score")).toBe(
      "No passage scored high enough to answer this confidently — try naming the product or adding a specific detail.",
    );
  });

  it("stays silent for reasons whose reply already says the why", () => {
    // Audit #1715: the card carried the backend's "I couldn't identify the
    // product..." and then restated it underneath in the monospace diagnostic
    // register. The conversational outcomes own their copy; no_product is here
    // as read-only legacy for turns persisted before they existed.
    expect(reasonCopy("need_product")).toBeNull();
    expect(reasonCopy("product_not_covered")).toBeNull();
    expect(reasonCopy("no_product")).toBeNull();
  });

  it("never exposes an unknown internal reason code", () => {
    expect(reasonCopy("some_future_code")).toBe(
      "The request could not be completed as expected.",
    );
  });

  it("explains technical and safety failures in plain language", () => {
    expect(reasonCopy("pipeline_error")).toBe(
      "An internal processing step could not be completed.",
    );
    expect(reasonCopy("malformed_structure")).toBe(
      "The model response could not be validated.",
    );
    expect(reasonCopy("material_drop")).toBe(
      "A rationale question meets a guidance that documents requirements, not reasoning.",
    );
  });

  it("returns null when there is no reason", () => {
    expect(reasonCopy(null)).toBeNull();
  });
});

describe("nonAnswerLabel — neutral, reason-aware outcomes", () => {
  it("distinguishes evidence gaps, technical unavailability, and scope", () => {
    expect(nonAnswerLabel("refused", true, "low_top_score")).toBe("Evidence gap");
    expect(nonAnswerLabel("error", true, "provider_error")).toBe("Answer unavailable");
    expect(nonAnswerLabel("scope_warning", true, "scope_warning")).toBe("Out of scope");
  });

  // Every infrastructure fault must read "Answer unavailable" even when the
  // backend delivered it as a refusal (status="refused") -- an outage is a
  // system fault, not a claim that the corpus is silent.
  it.each([
    "provider_error",
    "empty_completion",
    "upstream_error",
    "catalog_error",
    "pipeline_error",
    "audit_error",
    "malformed_structure",
  ])("labels a refused turn with reason=%s as Answer unavailable", (reason) => {
    expect(nonAnswerLabel("refused", true, reason)).toBe("Answer unavailable");
  });

  it("keeps evidence-gap reasons on the epistemic label", () => {
    for (const reason of [
      "retrieval",
      "low_top_score",
      "no_valid_citations",
      "material_drop",
      "model_refusal",
      "spine_unresolved",
    ]) {
      expect(nonAnswerLabel("refused", true, reason)).toBe("Evidence gap");
    }
  });
});

function nonAnswerTurn(overrides: Partial<Turn>): Turn {
  return {
    role: "assistant",
    content: "Try **asking about one product** or review [FDA guidance](https://www.fda.gov/).",
    status: "refused",
    refused: true,
    citations: [],
    clarify: [],
    related: [],
    interpretation: null,
    reason: "low_top_score",
    live: true,
    meta: null,
    createdAt: null,
    id: null,
    statusLog: [],
    streamFellBack: false,
    draftWithdrawn: null,
    ...overrides,
  };
}

describe("AssistantTurn — helpful non-answer rendering", () => {
  it("keeps the canonical clarification body, with a distinct interpretation as a caption", () => {
    const turn = nonAnswerTurn({
      status: "clarify",
      refused: false,
      content: "Please choose **one dosage form**.",
      interpretation: "You are asking about metformin.",
      reason: "multi_form",
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );

    expect(container.querySelector(".msg__body")?.textContent).toContain("Please choose one dosage form.");
    expect(container.querySelector(".msg__body strong")?.textContent).toBe("one dosage form");
    expect(container.querySelector(".msg__interp")?.textContent).toBe(
      "Interpreted as: You are asking about metformin.",
    );
    expect(container.querySelector(".cite-stamp")).toBeNull();
  });

  it("renders evidence-gap guidance as Markdown without citation affordances", () => {
    const { container, getByRole } = render(
      <AssistantTurn
        turn={nonAnswerTurn({})}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );

    expect(container.querySelector(".msg__declined-tag")?.textContent).toBe("Evidence gap");
    expect(container.querySelector(".msg__body strong")?.textContent).toBe("asking about one product");
    expect(getByRole("link", { name: "FDA guidance" })).toHaveAttribute("href", "https://www.fda.gov/");
    expect(container.querySelector(".cite-stamp")).toBeNull();
    expect(container.querySelector(".cite")).toBeNull();
  });

  it("keeps scope warnings distinct from unavailable answers", () => {
    const { container, rerender } = render(
      <AssistantTurn
        turn={nonAnswerTurn({ status: "scope_warning", reason: "scope_warning" })}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".msg__declined-tag")?.textContent).toBe("Out of scope");

    rerender(
      <AssistantTurn
        turn={nonAnswerTurn({ status: "error", reason: "provider_error" })}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".msg__declined-tag")?.textContent).toBe("Answer unavailable");
  });

  it("adds the transient-retry cue on an infrastructure failure", () => {
    const { container } = render(
      <AssistantTurn
        turn={nonAnswerTurn({ reason: "upstream_error" })}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".msg__declined .msg__retry")?.textContent).toBe(
      "Likely transient \u2014 try the question again in a moment.",
    );
  });

  it("never suggests a retry on an evidence gap (re-asking won't grow the corpus)", () => {
    const { container } = render(
      <AssistantTurn
        turn={nonAnswerTurn({ reason: "low_top_score" })}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".msg__declined-tag")?.textContent).toBe("Evidence gap");
    expect(container.querySelector(".msg__retry")).toBeNull();
  });
});

// Params are lowerCamelCase (style); the RETURNED keys stay wire-shape snake.
function wireCitation(shortName: string, page: number): Citation {
  return {
    short_name: shortName,
    page,
    chunk_id: `${shortName}-${page}`,
    doc_id: 1,
    version_id: 1,
    source_url: "https://example.test/doc.pdf",
    snippet: "snippet text",
  };
}

describe("AssistantTurn -- ungrounded answer backstop (INV-1)", () => {
  const ungrounded = nonAnswerTurn({
    status: "answer",
    refused: false,
    reason: null,
    content: "An uncited claim.",
  });

  it("renders the loud anomaly block instead of whisper meta text", () => {
    const { container, getByRole } = render(
      <AssistantTurn turn={ungrounded} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    const note = getByRole("note");
    expect(note.className).toContain("msg__ungrounded");
    expect(container.querySelector(".msg__ungrounded-tag")?.textContent).toBe(
      "Ungrounded \u2014 treat as unverified",
    );
    expect(container.textContent).toContain("no supporting citations");
    // The old whisper fallback is gone.
    expect(container.textContent).not.toContain("No citations");
  });

  it("still exposes zero grounding affordances (warns, never fabricates)", () => {
    const { container } = render(
      <AssistantTurn turn={ungrounded} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelector(".cite")).toBeNull();
    expect(container.querySelector(".cite-stamp")).toBeNull();
    expect(container.querySelector(".confidence")).toBeNull();
  });
});

describe("AssistantTurn -- duplicated wire citations collapse to one display list", () => {
  it("de-dupes chips, the Sources count, and reference rows together", () => {
    const turn = nonAnswerTurn({
      status: "answer",
      refused: false,
      reason: null,
      content: "A cited claim.",
      citations: [
        wireCitation("PSG_020503", 3),
        wireCitation("PSG_020503", 3),
        wireCitation("PSG_021730", 4),
      ],
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelectorAll(".cite")).toHaveLength(2);
    expect(container.querySelector(".sources__count")?.textContent).toBe("2 sources");
    expect(container.querySelectorAll(".ref")).toHaveLength(2);
    // Duplicates never demote the turn to the ungrounded register.
    expect(container.querySelector(".msg__ungrounded")).toBeNull();
  });

  it("labels reference PDF links and states a missing revision date", () => {
    const turn = nonAnswerTurn({
      status: "answer",
      refused: false,
      reason: null,
      content: "A cited claim.",
      citations: [wireCitation("PSG_020503", 3)],
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    const link = container.querySelector<HTMLAnchorElement>(".ref a");
    // Labeled action instead of raw-URL soup; target/href untouched.
    expect(link?.textContent).toContain("Open source PDF");
    expect(link?.getAttribute("href")).toBe("https://example.test/doc.pdf");
    // wireCitation carries no recency fields \u2014 the row must say so explicitly.
    expect(container.querySelector(".ref .recency__none")?.textContent).toBe(
      "Revision date not recorded",
    );
  });
});

describe("rehydrated error turns — the declined register survives a reload (INV-2)", () => {
  // The wire shape from GET /sessions/{id} carries NO refused flag; the backend
  // persists provider-failure refusals as status="error" with audit_id set.
  const errorMessage = {
    id: "m2",
    turn_id: "t2",
    role: "assistant",
    content: "The model provider failed to respond, so this question was not answered.",
    status: "error",
    citations: [],
    audit_id: 7,
    reason: "provider_error",
    created_at: "2026-01-01T00:00:00Z",
  } as unknown as ChatMessage;

  it("maps status=error back to refused, matching the live wire's refused=true", () => {
    expect(turnFromMessage(errorMessage).refused).toBe(true);
  });

  it("renders in the declined register — never dressed as a cited answer", () => {
    const { container } = render(
      <AssistantTurn
        turn={turnFromMessage(errorMessage)}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".msg__declined")).not.toBeNull();
    // The answer-register furniture must not appear: no ungrounded-anomaly
    // block (the cited branch's INV-1 backstop) and no citation chips.
    expect(container.textContent).not.toContain("Ungrounded");
    expect(container.querySelector(".msg__ungrounded")).toBeNull();
    expect(container.querySelector(".cite")).toBeNull();
  });
});

describe("ProvisionalDraft — the streaming draft (INV-1/INV-2)", () => {
  it("shows the streamed text with no grounding affordances", () => {
    const { container } = render(<ProvisionalDraft text="A fasting study [PSG_020503, p.3]." />);
    // The marker stays literal (never a clickable stamp).
    expect(container.querySelector(".msg__body--draft")?.textContent).toBe(
      "A fasting study [PSG_020503, p.3].",
    );
    // No citation chip, stamp, or confidence band appears before validation.
    expect(container.querySelector(".cite")).toBeNull();
    expect(container.querySelector(".cite-stamp")).toBeNull();
    expect(container.querySelector(".confidence")).toBeNull();
  });

  it("captions the draft with a bare 'Drafting', not a sentence about verification", () => {
    const { container } = render(<ProvisionalDraft text="Half a sent" />);
    const caption = container.querySelector(".msg__drafting");
    // textContent includes the aria-hidden dot span, which contributes nothing.
    expect(caption?.textContent).toBe("Drafting");
    // The old caption promised the verified answer in the visual layer; that
    // promise now lives only in the aria-live milestone (page.tsx), since the
    // whole draft row is aria-hidden.
    expect(container.textContent).not.toContain("verified answer follows");
    // The pulsing dot is the liveness signal and stays decorative.
    expect(container.querySelector(".msg__drafting-dot")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("renders as markdown (same typography it settles into) with zero interactive stamps", () => {
    const { container, queryAllByRole } = render(
      <ProvisionalDraft text={"A **bold** fasting claim [PSG_020503, p.3]."} />,
    );
    // Markdown emphasis renders; without the citation-less <Markdown> path the
    // draft would be raw text and this <strong> would not exist.
    expect(container.querySelector(".msg__body--draft strong")?.textContent).toBe("bold");
    // Verified zero-stamp path: stampable requires citations + onCite.
    expect(queryAllByRole("button")).toHaveLength(0);
    expect(container.textContent).toContain("[PSG_020503, p.3]");
  });

  it("renders a markdown link as inert text -- no anchor, no clickable affordance", () => {
    const { container } = render(
      <ProvisionalDraft text={"See [FDA guidance](https://www.fda.gov/) for details."} />,
    );
    const draft = container.querySelector(".msg__body--draft");
    // Unvalidated output must never carry the sourced register or a clickable
    // gold anchor: the link collapses to plain text until validation.
    expect(draft?.querySelector("a")).toBeNull();
    expect(draft?.textContent).toContain("FDA guidance");
  });
});

const FIXTURE_META = { model_name: "test-model", audit_id: 4218, turn_id: "t9" };
const FILED_AT = "2026-01-07T14:32:00Z";

describe("the docket margin -- provenance rail on assistant turns (B9)", () => {
  const answerWithProvenance = nonAnswerTurn({
    status: "answer",
    refused: false,
    reason: null,
    content: "A cited claim.",
    citations: [{ ...wireCitation("PSG_020503", 3), score: 0.71 }],
    createdAt: FILED_AT,
    meta: FIXTURE_META,
  });

  it("renders time filed and a high-confidence dot in an aria-hidden rail, with no audit stamp", () => {
    const { container } = render(
      <AssistantTurn
        turn={answerWithProvenance}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    const margin = container.querySelector(".chat-row__margin");
    expect(margin).not.toBeNull();
    expect(margin?.querySelector(".avatar")).not.toBeNull();
    // Decorative restatement of data already announced by the body/audit line.
    const marginalia = margin?.querySelector(".marginalia");
    expect(marginalia?.getAttribute("aria-hidden")).toBe("true");
    expect(marginalia?.querySelector(".marginalia__time")?.textContent).toBe(formatClock(FILED_AT));
    // The "#<audit_id>" stamp is gone: an id beside every reply read as a case
    // file. The value is still on the visible audit line and in Provenance.
    expect(marginalia?.querySelector(".marginalia__audit")).toBeNull();
    expect(marginalia?.textContent).not.toContain("4218");
    expect(container.querySelector(".prov__grid")?.textContent).toContain("#4218");
    expect(marginalia?.querySelector(".marginalia__dot--high")).not.toBeNull();
    expect(marginalia?.querySelector(".marginalia__dot--moderate")).toBeNull();
  });

  it("shows the moderate dot for a near-threshold answer", () => {
    const turn = nonAnswerTurn({
      status: "answer",
      refused: false,
      reason: null,
      content: "A hedged claim.",
      citations: [{ ...wireCitation("PSG_020503", 3), score: 0.41 }],
      createdAt: FILED_AT,
      meta: FIXTURE_META,
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelector(".marginalia__dot--moderate")).not.toBeNull();
    expect(container.querySelector(".marginalia__dot--high")).toBeNull();
  });

  it("renders no confidence dot on a declined turn (a non-answer never wears one)", () => {
    const turn = nonAnswerTurn({ createdAt: FILED_AT, meta: FIXTURE_META });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    // Time + audit still file into the margin; only the confidence mark is absent.
    expect(container.querySelector(".marginalia")).not.toBeNull();
    expect(container.querySelector(".marginalia__dot")).toBeNull();
  });

  it("renders no dot on a refused turn even when a scored citation leaked onto it (INV-2 defense-in-depth)", () => {
    // The backend empties citations on refusal, so this shape should be
    // unreachable -- but the dot is gated on status, not on that upstream
    // guarantee: a refused turn carrying a scored citation still shows none.
    const turn = nonAnswerTurn({
      createdAt: FILED_AT,
      meta: FIXTURE_META,
      citations: [{ ...wireCitation("PSG_020503", 3), score: 0.71 }],
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelector(".marginalia")).not.toBeNull();
    expect(container.querySelector(".marginalia__dot")).toBeNull();
  });

  it("keeps the bare avatar when a turn has no provenance at all", () => {
    const { container } = render(
      <AssistantTurn
        turn={nonAnswerTurn({})}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".chat-row__margin .avatar")).not.toBeNull();
    expect(container.querySelector(".marginalia")).toBeNull();
  });

  it("adds a labelled Filed row to the provenance record when createdAt parses", () => {
    const { container } = render(
      <AssistantTurn
        turn={answerWithProvenance}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    const filed = container.querySelector(".prov__line");
    expect(filed?.querySelector("dt")?.textContent).toBe("Filed");
    expect(filed?.textContent).toContain(formatFiled(FILED_AT));
  });

  it("names the provenance rows and shortens the trace instead of dumping ids", () => {
    const { container } = render(
      <AssistantTurn
        turn={{ ...answerWithProvenance, meta: { ...FIXTURE_META, turn_id: "157314a8-33ef-4f29" } }}
        sessionId="330d61ef-4540"
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    const grid = container.querySelector(".prov__grid");
    const rows = Array.from(grid?.querySelectorAll(".prov__row") ?? []).map(
      (r) => [r.querySelector("dt")?.textContent, r.querySelector("dd")?.textContent] as const,
    );
    expect(rows).toContainEqual(["Audit", "#4218"]);
    expect(rows).toContainEqual(["Model", "test-model"]);
    // The trace shows only a recognizable prefix; the full ids ride in the
    // title attribute for support conversations, and the session id is never
    // restated as visible text.
    expect(rows).toContainEqual(["Trace", "157314a8"]);
    const trace = Array.from(grid?.querySelectorAll(".prov__row dd") ?? []).find(
      (d) => d.textContent === "157314a8",
    );
    expect(trace?.getAttribute("title")).toBe(
      "turn 157314a8-33ef-4f29 · session 330d61ef-4540",
    );
    expect(grid?.textContent).not.toContain("330d61ef-4540");
  });
});

describe("bibliography-style answers -- trailer split + bare markers (INV-1)", () => {
  const biblioTurn = nonAnswerTurn({
    status: "answer",
    refused: false,
    reason: null,
    content:
      "A crossover study is recommended [1]. In vitro studies apply too [2], but [3] has no entry.\n\nSources:\n[1] [PSG_020503, p.3]\n[2] [PSG_020503, p.5]",
    citations: [
      { ...wireCitation("PSG_020503", 3), score: 0.71 },
      { ...wireCitation("PSG_020503", 5), score: 0.6 },
    ],
    meta: FIXTURE_META,
  });

  it("drops the model-authored Sources trailer from the body (the UI's references replace it)", () => {
    const { container } = render(
      <AssistantTurn turn={biblioTurn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelector(".msg__body")?.textContent).not.toContain("Sources:");
    // The real reference list is still there, numbered from the validated set.
    expect(container.querySelector(".sources__count")?.textContent).toBe("2 sources");
    expect(container.querySelectorAll(".ref")).toHaveLength(2);
  });

  it("resolves listed bare markers into stamps and leaves unlisted ones literal", () => {
    const onCite = vi.fn();
    const { container } = render(
      <AssistantTurn turn={biblioTurn} sessionId={null} onPick={() => {}} onCite={onCite} busy={false} threshold={null} />,
    );
    const stamps = Array.from(container.querySelectorAll(".cite-stamp"));
    expect(stamps.map((s) => s.textContent)).toEqual(["[1]", "[2]"]);
    // [3] names no bibliography entry: never fabricated, stays prose.
    expect(container.querySelector(".msg__body")?.textContent).toContain("[3]");
    fireEvent.click(stamps[1]);
    expect(onCite).toHaveBeenCalledWith(expect.objectContaining({ short_name: "PSG_020503", page: 5 }));
  });

  it("keeps the full text (trailer included) when a turn carries no citations", () => {
    const uncited = nonAnswerTurn({
      status: "answer",
      refused: false,
      reason: null,
      content: "An unverifiable claim.\n\nSources:\n[1] [PSG_999999, p.9]",
      citations: [],
      meta: FIXTURE_META,
    });
    const { container } = render(
      <AssistantTurn turn={uncited} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    // The ungrounded backstop fires AND the reply's own source hints survive --
    // with no reference list below, stripping would erase them.
    expect(container.querySelector(".msg__ungrounded")).not.toBeNull();
    expect(container.querySelector(".msg__body")?.textContent).toContain("Sources:");
  });
});

describe("feedback on refusals and clarifies (D18)", () => {
  beforeEach(() => {
    sendFeedbackMock.mockClear();
  });

  it("renders the declined-variant copy on a refusal that carries an audit id", () => {
    const { getByText, getByRole } = render(
      <AssistantTurn
        turn={nonAnswerTurn({ meta: FIXTURE_META })}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(getByText("Should this have been answered?")).not.toBeNull();
    expect(getByRole("button", { name: "Rightly declined" })).not.toBeNull();
    expect(getByRole("button", { name: "Should have answered" })).not.toBeNull();
  });

  it("renders no feedback on a refusal without an audit id (nothing to rate against)", () => {
    const { container } = render(
      <AssistantTurn
        turn={nonAnswerTurn({})}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".fb")).toBeNull();
  });

  it("renders the clarify-variant copy at the end of a clarify turn with meta", () => {
    const turn = nonAnswerTurn({
      status: "clarify",
      refused: false,
      reason: "multi_form",
      content: "Which one?",
      clarify: [{ label: "option a", query: "option a", filters: {} }],
      meta: FIXTURE_META,
    });
    const { getByText, getByRole } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(getByText("Was this clarification needed?")).not.toBeNull();
    expect(getByRole("button", { name: "Good ask" })).not.toBeNull();
    expect(getByRole("button", { name: "Unnecessary" })).not.toBeNull();
  });

  it("posts -1 with no comment when 'Should have answered' is clicked", async () => {
    const { getByRole, findByText } = render(
      <AssistantTurn
        turn={nonAnswerTurn({ meta: FIXTURE_META })}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    fireEvent.click(getByRole("button", { name: "Should have answered" }));
    // Wait for the saved status so the async POST settles inside the test.
    await findByText(/Noted/);
    expect(sendFeedbackMock).toHaveBeenCalledTimes(1);
    expect(sendFeedbackMock).toHaveBeenCalledWith(4218, -1, null);
  });

  it("keeps the answer-branch feedback copy unchanged (regression)", () => {
    const turn = nonAnswerTurn({
      status: "answer",
      refused: false,
      reason: null,
      content: "A cited claim.",
      citations: [wireCitation("PSG_020503", 3)],
      meta: FIXTURE_META,
    });
    const { getByText, getByRole } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(getByText("Assess")).not.toBeNull();
    expect(getByRole("button", { name: "Helpful" })).not.toBeNull();
    expect(getByRole("button", { name: "Not helpful" })).not.toBeNull();
  });
});

describe("stream-fallback notice + provenance status log (B8)", () => {
  const citedOverrides: Partial<Turn> = {
    status: "answer",
    refused: false,
    reason: null,
    content: "A cited claim.",
    citations: [wireCitation("PSG_020503", 3)],
    meta: FIXTURE_META,
  };

  it("explains the vanished draft when the turn settled through the /query fallback", () => {
    const turn = nonAnswerTurn({ ...citedOverrides, streamFellBack: true });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelector(".msg__fallback")?.textContent).toBe(
      "Connection dropped mid-draft \u2014 the answer was re-run over a fresh request and may differ from the draft.",
    );
  });

  it("shows the notice on a declined turn too (a fallback can settle into a refusal)", () => {
    const { container } = render(
      <AssistantTurn
        turn={nonAnswerTurn({ streamFellBack: true, meta: FIXTURE_META })}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".msg__fallback")).not.toBeNull();
  });

  it("renders no notice when the stream held (streamFellBack false)", () => {
    const { container } = render(
      <AssistantTurn
        turn={nonAnswerTurn(citedOverrides)}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false} threshold={null}
      />,
    );
    expect(container.querySelector(".msg__fallback")).toBeNull();
  });

  it("prints no status-frame docket in Provenance even when the turn carries frames", () => {
    // The SSE frames narrate the machine's own progress; a numbered log under
    // every reply made the record read like a pipeline transcript. Provenance
    // keeps only what an analyst can act on (Audit / Model / Filed / Trace).
    const turn = nonAnswerTurn({
      ...citedOverrides,
      statusLog: ["Resolving product...", "Searching 1,795 documents..."],
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelector(".prov__log")).toBeNull();
    expect(container.querySelector(".prov ol")).toBeNull();
    expect(container.querySelector(".prov")?.textContent).not.toContain("Resolving product...");
  });

  it("keeps the acted-on Provenance rows (the Model row is the only record of the engine)", () => {
    const turn = nonAnswerTurn({
      ...citedOverrides,
      statusLog: ["Resolving product..."],
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    const labels = [...container.querySelectorAll(".prov__row dt")].map((dt) => dt.textContent);
    expect(labels).toContain("Audit");
    expect(labels).toContain("Model");
    expect(labels).toContain("Trace");
  });
});

describe("confidenceTitle -- the band grounded in the live threshold (A4)", () => {
  it("anchors High to the fixed 0.55 cut regardless of the threshold", () => {
    expect(confidenceTitle("High", 0.3)).toBe("Top passage score at or above 0.55");
    expect(confidenceTitle("High", null)).toBe("Top passage score at or above 0.55");
  });

  it("names the live refusal threshold on Moderate, formatted to two decimals", () => {
    expect(confidenceTitle("Moderate", 0.3)).toBe(
      "Above the refusal threshold (0.30), below 0.55",
    );
    expect(confidenceTitle("Moderate", 0.455)).toBe(
      "Above the refusal threshold (0.46), below 0.55",
    );
  });

  it("degrades to the bare cut before /settings resolves (never fakes a number)", () => {
    expect(confidenceTitle("Moderate", null)).toBe("Below 0.55");
  });
});

describe("AssistantTurn -- confidence legend on cited answers (A4)", () => {
  const scoredTurn = nonAnswerTurn({
    status: "answer",
    refused: false,
    reason: null,
    content: "A cited claim.",
    citations: [{ ...wireCitation("PSG_020503", 3), score: 0.41 }],
  });

  it("titles the band with the threshold-grounded explanation", () => {
    const { container } = render(
      <AssistantTurn
        turn={scoredTurn}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false}
        threshold={0.3}
      />,
    );
    const band = container.querySelector(".confidence--moderate");
    expect(band?.getAttribute("title")).toContain("0.30");
    expect(band?.getAttribute("title")).toContain("below 0.55");
  });

  it("restates the legend in a sr-only span (the title attr is mouse-only)", () => {
    const { container } = render(
      <AssistantTurn
        turn={scoredTurn}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false}
        threshold={0.3}
      />,
    );
    // Keyboard, touch, and SR users cannot reach a title attribute; the same
    // explanation must be in the accessibility tree.
    const srText = container.querySelector(".confidence .sr-only")?.textContent;
    expect(srText).toContain("0.30");
    expect(srText).toContain("below 0.55");
  });

  it("renders 'Confidence not recorded' when citations carry no scores", () => {
    const turn = nonAnswerTurn({
      status: "answer",
      refused: false,
      reason: null,
      content: "A cited claim.",
      // wireCitation has no score field -- the score-less rehydrated shape.
      citations: [wireCitation("PSG_020503", 3)],
    });
    const { container } = render(
      <AssistantTurn
        turn={turn}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false}
        threshold={0.3}
      />,
    );
    const none = container.querySelector(".confidence.confidence--none");
    expect(none?.textContent).toContain("Confidence not recorded");
    // The explicit-absence row must not wear a band color.
    expect(container.querySelector(".confidence--high")).toBeNull();
    expect(container.querySelector(".confidence--moderate")).toBeNull();
    // Absence still shares the quiet row, so an unscored answer is laid out
    // exactly like a scored one -- only the words differ.
    expect(container.querySelector(".sources__row .confidence--none")).not.toBeNull();
    expect(container.querySelector(".sources__count")?.textContent).toBe("1 source");
  });
});

describe("AssistantTurn -- the quiet row (band + source count on one line)", () => {
  const scoredTurn = nonAnswerTurn({
    status: "answer",
    refused: false,
    reason: null,
    content: "A cited claim.",
    citations: [
      { ...wireCitation("PSG_020503", 3), score: 0.41 },
      { ...wireCitation("PSG_021730", 4), score: 0.41 },
    ],
    meta: FIXTURE_META,
  });

  it("puts the band and the source count in the same Sources summary element", () => {
    const { container } = render(
      <AssistantTurn
        turn={scoredTurn}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false}
        threshold={0.3}
      />,
    );
    const row = container.querySelector(".sources > summary.sources__row");
    expect(row).not.toBeNull();
    expect(row?.querySelector(".confidence")?.textContent).toContain("Moderate confidence");
    expect(row?.querySelector(".sources__count")?.textContent).toBe("2 sources");
    // Exactly one such row per cited turn -- the confidence line no longer has
    // a block of its own above the chips.
    expect(container.querySelectorAll(".confidence")).toHaveLength(1);
    expect(container.querySelectorAll(".sources__row")).toHaveLength(1);
  });

  it("keeps the chips above it -- the only above-the-fold human source names", () => {
    const { container } = render(
      <AssistantTurn
        turn={scoredTurn}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false}
        threshold={0.3}
      />,
    );
    const chips = container.querySelector(".cites");
    const row = container.querySelector(".sources");
    expect(chips).not.toBeNull();
    expect(container.querySelectorAll(".cite")).toHaveLength(2);
    // DOCUMENT_POSITION_FOLLOWING === 4: the row comes after the chips.
    expect(chips?.compareDocumentPosition(row as Node)).toBe(4);
  });

  it("renders the row on a one-citation answer too (presence never encodes length)", () => {
    // The row is unconditional on a cited turn: an analyst must not be able to
    // read "short answer" off its absence, or "long answer" off its presence.
    const short = nonAnswerTurn({
      status: "answer",
      refused: false,
      reason: null,
      content: "Yes.",
      citations: [{ ...wireCitation("PSG_020503", 3), score: 0.71 }],
      meta: FIXTURE_META,
    });
    const { container } = render(
      <AssistantTurn
        turn={short}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false}
        threshold={0.3}
      />,
    );
    const row = container.querySelector("summary.sources__row");
    expect(row?.querySelector(".confidence--high")?.textContent).toContain("High confidence");
    expect(row?.querySelector(".sources__count")?.textContent).toBe("1 source");
  });
});

describe("clarify audit visibility", () => {
  it("shows the audit line on a clarify turn with meta (not only inside the aria-hidden rail)", () => {
    const turn = nonAnswerTurn({
      status: "clarify",
      refused: false,
      reason: "multi_form",
      content: "Which one?",
      clarify: [{ label: "option a", query: "option a", filters: {} }],
      meta: FIXTURE_META,
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelector(".msg__audit")?.textContent).toContain("#4218");
  });

  it("renders no audit line on a clarify turn without meta", () => {
    const turn = nonAnswerTurn({
      status: "clarify",
      refused: false,
      reason: "multi_form",
      content: "Which one?",
    });
    const { container } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    expect(container.querySelector(".msg__audit")).toBeNull();
  });
});

describe("clarify options group (D19)", () => {
  it("names the clarify pills as a group for screen readers", () => {
    const turn = nonAnswerTurn({
      status: "clarify",
      refused: false,
      reason: "multi_form",
      content: "Which one?",
      clarify: [
        { label: "option a", query: "option a", filters: {} },
        { label: "option b", query: "option b", filters: {} },
      ],
    });
    const { getByRole } = render(
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} threshold={null} />,
    );
    const group = getByRole("group", { name: "Clarification options" });
    expect(group.querySelectorAll(".pill")).toHaveLength(2);
  });
});
