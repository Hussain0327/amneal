import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import StudioPage from "@/app/studio/page";

// The studio is a desktop instrument with a lot of measured layout. jsdom gives
// it none of that, so the surface has to stay upright without ResizeObserver,
// without scrollTo/scrollIntoView, and with every box reporting zero. These
// tests exist as much to prove that as to check the behaviour.

beforeEach(() => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    // Reduced motion on: the assistant delivers whole replies instead of
    // streaming them word by word, which keeps these tests off fake timers.
    matches: query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function openPanel(name: RegExp) {
  return userEvent.click(screen.getByRole("button", { name }));
}

describe("Compliance Studio", () => {
  it("opens on the pre-checked specification with the repository listed", () => {
    render(<StudioPage />);

    expect(screen.getByText("Repository - 7 docs")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /3\.2\.S\.4\.1 Specification\.docx/ })).toBeInTheDocument();
    // The document itself is on the page, not just its file name in the tree.
    expect(screen.getByText(/3\.2\.S\.4\.1 Specification - Drug Substance/)).toBeInTheDocument();
  });

  it("puts the open document's findings on the spine, labelled", () => {
    render(<StudioPage />);

    const spine = screen.getByRole("group", { name: "Compliance spine" });
    const ticks = within(spine).getAllByRole("button");
    expect(ticks).toHaveLength(3);
    expect(
      within(spine).getByRole("button", { name: /Major finding at Header: Version control block is incomplete/ }),
    ).toBeInTheDocument();
  });

  it("counts only actionable findings on the rail badge", () => {
    render(<StudioPage />);
    // Two open (major + minor); the informational one is not something to fix.
    expect(screen.getByRole("button", { name: "Compliance results, 2 open findings" })).toBeInTheDocument();
  });

  it("reports a verdict and counts rather than a fabricated score", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);

    expect(screen.getByText("Resolve before filing")).toBeInTheDocument();
    expect(screen.getByText("2 to resolve")).toBeInTheDocument();
    expect(screen.getByText("1 note")).toBeInTheDocument();
    expect(screen.queryByText(/\/\s*100/)).not.toBeInTheDocument();
    expect(screen.queryByText(/compliance score/i)).not.toBeInTheDocument();
  });

  it("checks an unchecked document and blocks on the critical finding", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<StudioPage />);

    await user.click(screen.getByRole("button", { name: /3\.2\.P\.5\.1 Specification\.docx/ }));
    await openPanel(/^Compliance results/);
    expect(screen.getByText(/has not been checked yet/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Check this document" }));
    expect(screen.getByRole("button", { name: "Checking..." })).toBeDisabled();

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    expect(screen.getByText("Not submission-ready")).toBeInTheDocument();
    expect(screen.getByText("1 blocking")).toBeInTheDocument();
    expect(screen.getByText("Dissolution criterion omits staged testing")).toBeInTheDocument();
  });

  it("invalidates the findings under text the analyst edits", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    expect(screen.queryByText("Text edited - recheck")).not.toBeInTheDocument();

    // The header block carries the major finding about the version control block.
    const block = document.querySelector('[data-block-id="ds-2"]') as HTMLElement;
    expect(block).toBeTruthy();
    block.textContent = "Document ID: CMC-DS-SPEC-0007  |  Version: 6.0  |  Approved by: J. Patel";
    fireEvent.input(block);

    expect(screen.getByText("Text edited - recheck")).toBeInTheDocument();
    // A stale claim stops counting toward the verdict in either direction.
    expect(screen.getByText("1 stale")).toBeInTheDocument();
    expect(screen.getByText(/Edited since the last check/)).toBeInTheDocument();
    expect(screen.getByText(/Edited since last check - v5\.0/)).toBeInTheDocument();
  });

  it("answers in the assistant with its sources, and says it does not write", async () => {
    render(<StudioPage />);
    await openPanel(/Ask about this document/);

    expect(screen.getByText("Read-only. It explains and cites; it never edits your document.")).toBeInTheDocument();

    const box = screen.getByRole("textbox", { name: "Ask about this document" });
    await userEvent.type(box, "Who is the approver on this header?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.getByText("Who is the approver on this header?")).toBeInTheDocument();
    expect(screen.getByText(/Internal SOP QA-018 requires a document ID/)).toBeInTheDocument();
    expect(screen.getByText("QA-018 Document Control.docx - Section 1")).toBeInTheDocument();
  });

  it("declines rather than guessing when the corpus cannot answer", async () => {
    render(<StudioPage />);
    await openPanel(/Ask about this document/);

    const box = screen.getByRole("textbox", { name: "Ask about this document" });
    await userEvent.type(box, "What is our market share in Brazil?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.getByText(/I cannot answer that from this repository/)).toBeInTheDocument();
    expect(screen.getByText("No source in this repository.")).toBeInTheDocument();
  });

  it("filters the repository and gives direction when nothing matches", async () => {
    render(<StudioPage />);
    const search = screen.getByRole("textbox", { name: "Search documents" });

    await userEvent.type(search, "stability");
    expect(screen.getByRole("button", { name: /3\.2\.P\.8\.1 Stability Summary\.docx/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /AM-114 HPLC Assay\.docx/ })).not.toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, "zzzz");
    expect(screen.getByText("No documents match that search.")).toBeInTheDocument();
  });

  it("toggles the panel from the rail and closes it on Escape", async () => {
    render(<StudioPage />);
    const rail = screen.getByRole("button", { name: /Ask about this document/ });

    await userEvent.click(rail);
    expect(rail).toHaveAttribute("aria-pressed", "true");

    await userEvent.keyboard("{Escape}");
    expect(rail).toHaveAttribute("aria-pressed", "false");
  });

  it("leaves nothing focusable inside the closed panel", () => {
    render(<StudioPage />);
    const panel = document.querySelector(".st-panel") as HTMLElement;
    expect(panel).toHaveAttribute("aria-hidden", "true");
    expect(panel.querySelectorAll("button, a, input, textarea, [tabindex]")).toHaveLength(0);
  });

  it("marks edited text as a tracked change, and drops the marks when tracking is off", async () => {
    render(<StudioPage />);

    const block = document.querySelector('[data-block-id="ds-4"]') as HTMLElement;
    block.textContent = "Assay (on anhydrous basis) 97.0% - 102.0%; any unspecified impurity NMT 0.10%.";
    fireEvent.input(block);
    // The rewrite only lands once the caret leaves; editing under it would move it.
    fireEvent.blur(block);

    // The span starts after the common prefix "Assay (on anhydrous basis) 9",
    // so it opens on the digit that changed rather than on the whole number.
    const inserted = block.querySelector(".st-mark--insert");
    expect(inserted).toBeTruthy();
    expect(inserted?.textContent?.startsWith("7.0%")).toBe(true);

    await userEvent.click(screen.getByRole("button", { name: "Tracked changes" }));
    expect(block.querySelector(".st-mark--insert")).toBeNull();
    // Turning the switch off hides the marks; it must not touch the text.
    expect(block.textContent).toContain("97.0%");
  });

  it("keeps every table read-only, since cell offsets need a real editor", () => {
    render(<StudioPage />);
    for (const el of Array.from(document.querySelectorAll(".st-blk--table"))) {
      expect(el.getAttribute("contenteditable")).not.toBe("true");
    }
  });
});
