import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AssistantTurn, ProvisionalDraft, UserTurn } from "@/components/Turns";
import type { ChatMessage } from "@/lib/api";
import { nonAnswerLabel, reasonCopy, turnFromMessage, userTurn, type Turn } from "@/lib/turns";

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

  it("applies the .rise class only when the turn is live", () => {
    const { container, rerender } = render(<UserTurn content="hi" live />);
    expect(container.querySelector(".rise")).not.toBeNull();
    rerender(<UserTurn content="hi" live={false} />);
    expect(container.querySelector(".rise")).toBeNull();
  });
});

describe("reasonCopy — plain-language decline/clarify reasons", () => {
  it("maps a known backend reason code to analyst copy", () => {
    expect(reasonCopy("no_product")).toBe(
      "The product could not be identified confidently from this query.",
    );
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
      "The draft omitted a material qualifier, so it was withheld.",
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
      <AssistantTurn turn={turn} sessionId={null} onPick={() => {}} onCite={() => {}} busy={false} />,
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
        busy={false}
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
        busy={false}
      />,
    );
    expect(container.querySelector(".msg__declined-tag")?.textContent).toBe("Out of scope");

    rerender(
      <AssistantTurn
        turn={nonAnswerTurn({ status: "error", reason: "provider_error" })}
        sessionId={null}
        onPick={() => {}}
        onCite={() => {}}
        busy={false}
      />,
    );
    expect(container.querySelector(".msg__declined-tag")?.textContent).toBe("Answer unavailable");
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
        busy={false}
      />,
    );
    expect(container.querySelector(".msg__declined")).not.toBeNull();
    // The answer-register furniture must not appear: no "No citations" caption
    // (the defensive cited-branch fallback) and no citation chips.
    expect(container.textContent).not.toContain("No citations");
    expect(container.querySelector(".cite")).toBeNull();
  });
});

describe("ProvisionalDraft — the streaming draft (INV-1/INV-2)", () => {
  it("shows the raw streamed text with no grounding affordances", () => {
    const { container } = render(<ProvisionalDraft text="A fasting study [PSG_020503, p.3]." />);
    // Raw text; the marker stays literal (never a clickable stamp).
    expect(container.querySelector(".msg__body--draft")?.textContent).toBe(
      "A fasting study [PSG_020503, p.3].",
    );
    // No citation chip, stamp, or confidence band appears before validation.
    expect(container.querySelector(".cite")).toBeNull();
    expect(container.querySelector(".cite-stamp")).toBeNull();
    expect(container.querySelector(".confidence")).toBeNull();
    expect(container.textContent).toContain("verifying citations");
  });
});
