import { act, cleanup, createEvent, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import StudioPage from "@/app/studio/page";
import type { PsgLibraryDoc } from "@/lib/api";

// The page's only real-network import: the reference-library list. Runtime
// closures (the WatchPage pattern) so the hoisted factory never dereferences
// the mock before this module body runs.
const fetchPsgLibraryMock = vi.fn<() => Promise<PsgLibraryDoc[]>>();
vi.mock("@/lib/api", () => ({
  fetchPsgLibrary: () => fetchPsgLibraryMock(),
  psgPdfPath: (id: number) => `/api/psg/documents/${id}/pdf`,
}));

function libRow(over: Partial<PsgLibraryDoc> & { id: number }): PsgLibraryDoc {
  return {
    active_ingredient: "Albuterol Sulfate",
    normalized_name: "albuterol sulfate",
    stripped_name: "albuterol",
    dosage_form: "Aerosol, Metered",
    route: "Inhalation",
    appl_no: "020503",
    psg_type: "final",
    recommended_date: "2024-05-01",
    source_url: "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf",
    ...over,
  };
}

const LIB_ROWS: PsgLibraryDoc[] = [
  libRow({ id: 12 }),
  libRow({
    id: 13,
    active_ingredient: "Albuterol",
    dosage_form: "Tablet",
    route: "Oral",
    psg_type: "draft",
  }),
  libRow({
    id: 40,
    active_ingredient: "Budesonide",
    normalized_name: "budesonide",
    stripped_name: "budesonide",
    dosage_form: "Suspension",
    route: "Nasal",
  }),
];

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
  // Default: the library load never settles. The fixture-loop tests render
  // synchronously; a pending promise means zero post-render setState, so no
  // act() warnings and no assertion drift -- the section just shows its
  // loading line. Library tests override per-test.
  fetchPsgLibraryMock.mockReset();
  fetchPsgLibraryMock.mockImplementation(() => new Promise<never>(() => {}));
  // PdfPane's HEAD probe; jsdom's fetch would otherwise hit the real network.
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve({ ok: true, status: 200 })),
  );
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

    // The header block carries the major finding about the version control block.
    const block = document.querySelector('[data-block-id="ds-2"]') as HTMLElement;
    expect(block).toBeTruthy();
    block.textContent = "Document ID: CMC-DS-SPEC-0007  |  Version: 6.0  |  Approved by: J. Patel";
    fireEvent.input(block);

    // A stale claim stops counting toward the verdict in either direction.
    expect(screen.getByText("1 stale")).toBeInTheDocument();
    expect(screen.getByText(/Edited since the last check/)).toBeInTheDocument();
    expect(screen.getByText(/Edited since last check - v5\.0/)).toBeInTheDocument();
  });

  it("opens the Fixed gate only once the anchored text really changed", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);

    const card = () =>
      document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;
    const fixed = () => within(card()).getByRole("button", { name: "Fixed" });

    // Nothing edited yet: Fixed is refused, and it says why in visible text
    // rather than going quietly grey.
    expect(fixed()).toHaveAttribute("aria-disabled", "true");
    const gateId = fixed().getAttribute("aria-describedby");
    expect(gateId).toBeTruthy();
    expect(document.getElementById(gateId!)?.textContent).toMatch(
      /Fixed opens once you change the text this finding points at/,
    );

    const block = document.querySelector('[data-block-id="ds-2"]') as HTMLElement;
    block.textContent = "Document ID: CMC-DS-SPEC-0007  |  Version: 5.0  |  Approved by: J. Patel";
    fireEvent.input(block);

    expect(fixed()).not.toHaveAttribute("aria-disabled");
  });

  it("re-locks Fixed when the analyst types the edit back out again", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);

    const block = document.querySelector('[data-block-id="ds-2"]') as HTMLElement;
    const original = block.textContent ?? "";
    block.textContent = `${original}  |  Approved by: J. Patel`;
    fireEvent.input(block);
    const card = () => document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;
    expect(within(card()).getByRole("button", { name: "Fixed" })).not.toHaveAttribute("aria-disabled");

    // Typing it back is not a fix, and the gate has to notice.
    block.textContent = original;
    fireEvent.input(block);
    expect(within(card()).getByRole("button", { name: "Fixed" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("runs the whole loop: read, apply the suggested fix, mark it fixed", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);

    const card = () => document.querySelector('[data-finding-card="ds-f2"]') as HTMLElement;
    // The abbreviation finding is the one with an honest suggestion behind it.
    expect(within(card()).getByText("The limit of detection (LOD)")).toBeInTheDocument();
    expect(within(card()).getByRole("button", { name: "Fixed" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );

    await userEvent.click(within(card()).getByRole("button", { name: /Apply suggested fix/ }));

    // The document now carries the replacement, and the gate has opened.
    const block = document.querySelector('[data-block-id="ds-8"]') as HTMLElement;
    expect(block.textContent).toContain("The limit of detection (LOD) is applied");
    expect(within(card()).getByRole("button", { name: "Fixed" })).not.toHaveAttribute("aria-disabled");
    // Applying twice would double the text, so the button becomes the way back.
    expect(within(card()).queryByRole("button", { name: /Apply suggested fix/ })).not.toBeInTheDocument();

    await userEvent.click(within(card()).getByRole("button", { name: "Fixed" }));

    expect(within(card()).getByText("Fixed.")).toBeInTheDocument();
    expect(screen.getByText("1 recorded")).toBeInTheDocument();
    // Recorded findings stop counting as work: the rail badge drops to one.
    expect(screen.getByRole("button", { name: "Compliance results, 1 open finding" })).toBeInTheDocument();
  });

  it("restores the checked text and takes the fix claim back with it", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    const card = () => document.querySelector('[data-finding-card="ds-f2"]') as HTMLElement;

    await userEvent.click(within(card()).getByRole("button", { name: /Apply suggested fix/ }));
    await userEvent.click(within(card()).getByRole("button", { name: /Restore the checked text/ }));

    const block = document.querySelector('[data-block-id="ds-8"]') as HTMLElement;
    expect(block.textContent).toContain("Option I. LOD is applied");
    expect(within(card()).getByRole("button", { name: "Fixed" })).toHaveAttribute("aria-disabled", "true");
  });

  it("refuses a disposition with no justification and says what is missing", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    const card = () => document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;

    await userEvent.click(within(card()).getByRole("button", { name: "Disputed" }));
    const field = within(card()).getByRole("textbox", { name: /What does the check have wrong/ });
    expect(field).toHaveAttribute("aria-required", "true");
    expect(field).not.toHaveAttribute("aria-invalid");

    // The Record button is never disabled: pressing it is how you find out.
    await userEvent.click(within(card()).getByRole("button", { name: "Record" }));
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(within(card()).getByText(/Enter a justification before you record this/)).toBeInTheDocument();
    expect(screen.queryByText("1 recorded")).not.toBeInTheDocument();

    await userEvent.type(field, "The header does carry an approver on page 2.");
    await userEvent.click(within(card()).getByRole("button", { name: "Record" }));
    expect(within(card()).getByText("Disputed.")).toBeInTheDocument();
    expect(within(card()).getByText("The header does carry an approver on page 2.")).toBeInTheDocument();
  });

  it("keeps a half-typed justification through a document switch and back", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    const card = () => document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;

    await userEvent.click(within(card()).getByRole("button", { name: "Not applicable" }));
    await userEvent.type(
      within(card()).getByRole("textbox", { name: /Why is this not applicable/ }),
      "Draft in progress",
    );

    await userEvent.click(screen.getByRole("button", { name: /3\.2\.P\.5\.1 Specification\.docx/ }));
    await userEvent.click(screen.getByRole("button", { name: /3\.2\.S\.4\.1 Specification\.docx/ }));

    await userEvent.click(within(card()).getByRole("button", { name: "Not applicable" }));
    expect(within(card()).getByRole("textbox", { name: /Why is this not applicable/ })).toHaveValue(
      "Draft in progress",
    );
  });

  it("moves between open findings with F8 and skips the recorded ones", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);

    await userEvent.keyboard("{F8}");
    expect(document.querySelector('[data-finding-card="ds-f1"]')).toHaveClass("is-active");
    await userEvent.keyboard("{F8}");
    expect(document.querySelector('[data-finding-card="ds-f2"]')).toHaveClass("is-active");
    // ds-f3 is informational: an observation is not work, so traversal skips it.
    await userEvent.keyboard("{F8}");
    expect(document.querySelector('[data-finding-card="ds-f1"]')).toHaveClass("is-active");
  });

  it("leaves F8 alone while a justification has focus", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    const card = () => document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;

    await userEvent.click(within(card()).getByRole("button", { name: "Disputed" }));
    const field = within(card()).getByRole("textbox", { name: /What does the check have wrong/ });
    field.focus();
    await userEvent.keyboard("{F8}");
    expect(document.activeElement).toBe(field);
  });

  it("keeps every recorded disposition when the check runs again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    const card = () => document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;

    await user.click(within(card()).getByRole("button", { name: "Not applicable" }));
    await user.type(
      within(card()).getByRole("textbox", { name: /Why is this not applicable/ }),
      "Superseded by QA-018 rev 9.2.",
    );
    await user.click(within(card()).getByRole("button", { name: "Record" }));
    expect(within(card()).getByText("Not applicable.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Check this document" }));
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    // A re-check that wiped the dispositions would destroy the reason the
    // document is in the state it is.
    expect(within(card()).getByText("Not applicable.")).toBeInTheDocument();
    expect(within(card()).getByText("Superseded by QA-018 rev 9.2.")).toBeInTheDocument();
  });

  it("re-opens a fixed finding the checker still reports, keeping the record", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    const card = () => document.querySelector('[data-finding-card="ds-f2"]') as HTMLElement;

    await user.click(within(card()).getByRole("button", { name: /Apply suggested fix/ }));
    await user.click(within(card()).getByRole("button", { name: "Fixed" }));
    expect(within(card()).getByText("Fixed.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Check this document" }));
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    expect(within(card()).getByText(/The check ran again and still reports this/)).toBeInTheDocument();
    expect(within(card()).getByRole("button", { name: "Disputed" })).toBeInTheDocument();
  });

  it("shows the record as selectable text when the clipboard never answers", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    // Chrome queues a clipboard write until the document regains focus, so a
    // reviewer who clicks Copy and switches window can leave this pending
    // forever. A dead button is not an acceptable outcome for the one control
    // that gets their record out of the tool.
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: () => new Promise(() => {}) } });

    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    const card = () => document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;
    await user.click(within(card()).getByRole("button", { name: "Disputed" }));
    await user.type(
      within(card()).getByRole("textbox", { name: /What does the check have wrong/ }),
      "Stated on page 2.",
    );
    await user.click(within(card()).getByRole("button", { name: "Record" }));
    await user.click(screen.getByRole("button", { name: /Copy record/ }));

    await act(async () => {
      vi.advanceTimersByTime(2500);
    });

    const fallback = screen.getByRole("textbox", { name: "Copy this text" }) as HTMLTextAreaElement;
    expect(fallback.value).toContain("disputed");
    expect(fallback.value).toContain("Stated on page 2.");
    expect(fallback.value).toContain("Not a controlled record");
  });

  it("swallows Enter rather than welding two paragraphs together", () => {
    render(<StudioPage />);
    const block = document.querySelector('[data-block-id="ds-4"]') as HTMLElement;
    const event = createEvent.keyDown(block, { key: "Enter" });
    fireEvent(block, event);
    expect(event.defaultPrevented).toBe(true);
  });

  it("names each editable block by position, so typing does not rename it", async () => {
    render(<StudioPage />);
    const block = document.querySelector('[data-block-id="ds-4"]') as HTMLElement;
    const before = block.getAttribute("aria-label");
    expect(before).toBe("Paragraph 4 of 10");
    block.textContent = "Assay 97.0% only.";
    fireEvent.input(block);
    expect(block.getAttribute("aria-label")).toBe(before);
  });

  it("carries the working-record caveat wherever a record can be made", async () => {
    render(<StudioPage />);
    await openPanel(/^Compliance results/);
    const card = () => document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;

    await userEvent.click(within(card()).getByRole("button", { name: "Disputed" }));
    await userEvent.type(
      within(card()).getByRole("textbox", { name: /What does the check have wrong/ }),
      "Stated on page 2.",
    );
    await userEvent.click(within(card()).getByRole("button", { name: "Record" }));

    expect(
      screen.getByText(/Not a controlled record and not an electronic signature/),
    ).toBeInTheDocument();
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

describe("Reference library", () => {
  async function openAlbuterolPdf() {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    render(<StudioPage />);
    await userEvent.click(await screen.findByRole("button", { name: /^A - / }));
    await userEvent.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));
  }

  it("lists the database PSGs grouped by letter, drug and form", async () => {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    render(<StudioPage />);

    expect(
      await screen.findByRole("heading", { name: /Reference library - 3 PSGs/ }),
    ).toBeInTheDocument();
    // Letter buckets, collapsed by default, carrying their doc counts.
    const bucketA = await screen.findByRole("button", { name: /^A - / });
    expect(bucketA).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(bucketA);
    // Salt forms collapse under one drug ("Albuterol" holds both PSGs).
    expect(screen.getByRole("button", { name: /^Albuterol$/ })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\).*Final/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Tablet \(Oral\).*Draft/ })).toBeInTheDocument();
    // The working-documents header stays untouched.
    expect(screen.getByText("Repository - 7 docs")).toBeInTheDocument();
  });

  it("opens a PSG read-only in the inline viewer with the chrome hidden", async () => {
    await openAlbuterolPdf();

    const frame = await screen.findByTitle(/PSG PDF: Albuterol Sulfate/);
    expect(frame).toHaveAttribute("src", "/api/psg/documents/12/pdf");
    expect(screen.getByText("Read-only - FDA reference")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /PSG: Albuterol Sulfate/ })).toBeInTheDocument();
    // The compliance chrome describes the draft, so beside a PDF it is hidden.
    expect(screen.queryByRole("group", { name: "Compliance spine" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Compliance results/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Tracked changes" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check this document" })).toBeDisabled();
    expect(screen.getByText(/Compliance checks run on working documents/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Open on fda.gov" });
    expect(link).toHaveAttribute(
      "href",
      "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf",
    );
  });

  it("filters both sections from the one search box", async () => {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    render(<StudioPage />);
    await screen.findByRole("button", { name: /^A - / });

    const search = screen.getByRole("textbox", { name: "Search documents" });
    await userEvent.type(search, "albuterol");
    // No fixture draft matches; the library force-opens its surviving branch.
    expect(screen.getByText("No documents match that search.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\).*Final/ }),
    ).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, "specification");
    expect(
      screen.getByRole("button", { name: /3\.2\.S\.4\.1 Specification\.docx/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("No PSGs match that search.")).toBeInTheDocument();
  });

  it("shows a retry on load failure and leaves the working tree standing", async () => {
    fetchPsgLibraryMock.mockRejectedValueOnce(new Error("boom"));
    render(<StudioPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Couldn't load the reference library/);
    // The fixture repository is untouched by the failure.
    expect(screen.getByRole("button", { name: /3\.2\.S\.4\.1 Specification\.docx/ })).toBeInTheDocument();

    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: /^A - / })).toBeInTheDocument();
  });

  it("says when the database holds no guidance documents", async () => {
    fetchPsgLibraryMock.mockResolvedValue([]);
    render(<StudioPage />);
    expect(
      await screen.findByText("No FDA guidance documents ingested yet."),
    ).toBeInTheDocument();
  });

  it("leaves the disposition loop untouched across draft -> library -> draft", async () => {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    render(<StudioPage />);
    await userEvent.click(screen.getByRole("button", { name: /^Compliance results/ }));
    const card = () => document.querySelector('[data-finding-card="ds-f1"]') as HTMLElement;

    await userEvent.click(within(card()).getByRole("button", { name: "Not applicable" }));
    await userEvent.type(
      within(card()).getByRole("textbox", { name: /Why is this not applicable/ }),
      "Draft in progress",
    );

    // Into the library and back.
    await userEvent.click(await screen.findByRole("button", { name: /^A - / }));
    await userEvent.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));
    await screen.findByTitle(/PSG PDF: Albuterol Sulfate/);
    // F8 has nothing to traverse on a reference document.
    await userEvent.keyboard("{F8}");
    expect(document.querySelector("[data-finding-card]")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /3\.2\.S\.4\.1 Specification\.docx/ }));

    await userEvent.click(screen.getByRole("button", { name: /^Compliance results/ }));
    expect(within(card()).getByRole("textbox", { name: /Why is this not applicable/ })).toHaveValue(
      "Draft in progress",
    );
  });

  it("falls back to the fda.gov panel when the PDF probe fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, status: 404 })),
    );
    await openAlbuterolPdf();

    const fallback = await screen.findByRole("alert");
    expect(fallback).toHaveTextContent(/Couldn't load this PDF in the studio/);
    expect(screen.queryByTitle(/PSG PDF/)).not.toBeInTheDocument();
    expect(within(fallback).getByRole("link", { name: "Open on fda.gov" })).toBeInTheDocument();

    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: true, status: 200 })),
    );
    await userEvent.click(within(fallback).getByRole("button", { name: "Retry" }));
    expect(await screen.findByTitle(/PSG PDF: Albuterol Sulfate/)).toBeInTheDocument();
  });

  it("degrades Escape from the PDF back to the retained draft", async () => {
    await openAlbuterolPdf();
    await screen.findByTitle(/PSG PDF: Albuterol Sulfate/);

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByTitle(/PSG PDF/)).not.toBeInTheDocument();
    // The draft that was open before the detour is back on the canvas.
    expect(screen.getByText(/3\.2\.S\.4\.1 Specification - Drug Substance/)).toBeInTheDocument();
  });

  it("keeps Escape in the search box away from the PDF", async () => {
    await openAlbuterolPdf();
    await screen.findByTitle(/PSG PDF: Albuterol Sulfate/);

    await userEvent.click(screen.getByRole("textbox", { name: "Search documents" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.getByTitle(/PSG PDF: Albuterol Sulfate/)).toBeInTheDocument();
  });

  it("does not raise the findings panel or scrim when a check finishes behind an open PDF", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    render(<StudioPage />);

    // Start a check on the draft, then open a PSG before it completes.
    await user.click(screen.getByRole("button", { name: "Check this document" }));
    await user.click(await screen.findByRole("button", { name: /^A - / }));
    await user.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));
    await screen.findByTitle(/PSG PDF: Albuterol Sulfate/);

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    // The completed check must not put an invisible panel's scrim over the PDF.
    expect(screen.getByTitle(/PSG PDF: Albuterol Sulfate/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close panel" })).not.toBeInTheDocument();
  });
});
