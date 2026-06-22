import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UserTurn } from "@/components/Turns";
import type { ChatMessage } from "@/lib/api";
import { reasonCopy, turnFromMessage, userTurn } from "@/lib/turns";

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
    expect(reasonCopy("no_product")).toBe("No product in the corpus matched this query.");
  });

  it("falls back to the raw code for an unknown reason", () => {
    expect(reasonCopy("some_future_code")).toBe("some_future_code");
  });

  it("returns null when there is no reason", () => {
    expect(reasonCopy(null)).toBeNull();
  });
});
