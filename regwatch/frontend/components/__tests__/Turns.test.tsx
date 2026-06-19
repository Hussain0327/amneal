import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AssistantTurn } from "@/components/Turns";
import type { Citation } from "@/lib/api";
import type { Turn } from "@/lib/turns";

afterEach(cleanup);

const noop = () => {};

function cite(over: Partial<Citation> = {}): Citation {
  return {
    short_name: "Albuterol PSG",
    page: 3,
    chunk_id: "ch_1",
    doc_id: 1,
    version_id: 1,
    source_url: "https://www.accessdata.fda.gov/albuterol.pdf",
    snippet: "Conduct a single-dose, randomized, crossover BE study under fasting conditions.",
    ...over,
  };
}

function turn(over: Partial<Turn> = {}): Turn {
  return {
    role: "assistant",
    content: "Body.",
    status: "answer",
    refused: false,
    citations: [],
    clarify: [],
    interpretation: null,
    meta: null,
    ...over,
  };
}

function renderTurn(t: Turn, onCite: (c: Citation) => void = noop) {
  return render(<AssistantTurn turn={t} sessionId={null} onPick={noop} onCite={onCite} busy={false} />);
}

describe("AssistantTurn citation affordance", () => {
  it("renders a chip per citation on a cited answer turn", () => {
    const cites = [cite(), cite({ short_name: "Beclomethasone PSG", page: 7 })];
    const { container } = renderTurn(turn({ citations: cites }));

    expect(container.querySelectorAll(".cite")).toHaveLength(2);
    // The <details> Sources list (no-JS fallback) is still present AND still an
    // <a href> to the PDF — converting it to a button would fail this gate.
    expect(screen.getByText(/Sources · 2/)).toBeInTheDocument();
    const fallbackLink = container.querySelector(".sources a[href]");
    expect(fallbackLink).not.toBeNull();
    expect(fallbackLink).toHaveAttribute("href", cites[0].source_url);
  });

  it("opens the drawer with the exact citation when a chip is clicked", () => {
    const onCite = vi.fn();
    const cites = [cite(), cite({ short_name: "Beclomethasone PSG", page: 7 })];
    renderTurn(turn({ citations: cites }), onCite);

    fireEvent.click(screen.getByRole("button", { name: /Beclomethasone PSG · p\.7/ }));

    expect(onCite).toHaveBeenCalledTimes(1);
    expect(onCite).toHaveBeenCalledWith(cites[1]);
  });

  // INV-2 (refuse-over-guess): a declined turn is shown for what it is and never
  // carries a citation affordance — so the evidence drawer is unreachable from it.
  //
  // The declined fixtures deliberately carry a NON-EMPTY citations array. The wire
  // forces citations=[] on a refusal (_refuse()), but this test's job is to catch a
  // *code* regression — a chip leaked into the declined branch. With citations=[]
  // the branch would have nothing to map and the assertion would pass even when
  // broken (test theater). A real citation is what makes the gate bite.
  it("renders NO citation chip on a refused turn", () => {
    const { container } = renderTurn(turn({ status: "refused", refused: true, content: "Not in corpus.", citations: [cite()] }));
    expect(container.querySelector(".cite")).toBeNull();
    expect(screen.getByText(/Declined/)).toBeInTheDocument();
  });

  it("renders NO citation chip on a scope_warning turn", () => {
    const { container } = renderTurn(
      turn({ status: "scope_warning", content: "This isn't answerable from a PSG.", citations: [cite()] }),
    );
    expect(container.querySelector(".cite")).toBeNull();
    // Exact match -> the "Out of scope" declined tag, not substring-in-content.
    expect(screen.getByText("Out of scope")).toBeInTheDocument();
  });

  it("renders NO citation chip on a clarify turn", () => {
    const { container } = renderTurn(
      turn({
        status: "clarify",
        clarify: [{ label: "Albuterol", query: "albuterol" }],
        interpretation: "Which one?",
        citations: [cite()],
      }),
    );
    expect(container.querySelector(".cite")).toBeNull();
  });
});
