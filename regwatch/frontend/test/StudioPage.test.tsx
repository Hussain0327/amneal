import { act, cleanup, createEvent, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import StudioPage from "@/app/studio/page";
import type {
  PsgDocumentContent,
  PsgLibraryDoc,
  PsgRequirementsResponse,
} from "@/lib/api";

// The page's only real-network imports: the reference-library list and one
// PSG's text. Runtime closures (the WatchPage pattern) so the hoisted factory
// never dereferences the mocks before this module body runs.
const fetchPsgLibraryMock = vi.fn<() => Promise<PsgLibraryDoc[]>>();
const fetchPsgContentMock = vi.fn<(id: number) => Promise<PsgDocumentContent>>();
const fetchPsgRequirementsMock =
  vi.fn<(id: number) => Promise<PsgRequirementsResponse>>();
const askQueryMock = vi.fn<(q: string, f?: unknown) => Promise<QueryAnswer>>();
vi.mock("@/lib/api", () => ({
  fetchPsgLibrary: () => fetchPsgLibraryMock(),
  fetchPsgContent: (id: number) => fetchPsgContentMock(id),
  fetchPsgRequirements: (id: number) => fetchPsgRequirementsMock(id),
  askQuery: (q: string, f?: unknown) => askQueryMock(q, f),
  psgPdfPath: (id: number) => `/api/psg/documents/${id}/pdf`,
  psgDocxPath: (id: number) => `/api/psg/documents/${id}/docx`,
}));

type QueryAnswer = Awaited<ReturnType<typeof import("@/lib/api").askQuery>>;

function psgRequirements(
  over: Partial<PsgRequirementsResponse> = {},
): PsgRequirementsResponse {
  return {
    id: 12,
    extracted: true,
    requirements: [
      {
        key: "study_type",
        label: "Recommended study",
        value: "Bioequivalence",
        page: 1,
        quote: "Recommended Studies:",
      },
      {
        key: "study_design",
        label: "Study design",
        value: "Three in vitro studies",
        page: 2,
        quote: "Three in vitro bioequivalence studies",
      },
    ],
    ...over,
  };
}

function psgContent(over: Partial<PsgDocumentContent> = {}): PsgDocumentContent {
  return {
    id: 12,
    appl_no: "020503",
    file_name: "PSG_020503 Albuterol Sulfate.docx",
    active_ingredient: "Albuterol Sulfate",
    dosage_form: "Aerosol, Metered",
    route: "Inhalation",
    psg_type: "final",
    recommended_date: "2024-05-01",
    source_url: "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf",
    page_count: 2,
    truncated: false,
    blocks: [
      { id: "psg-12-b0", type: "title", text: "Guidance on Albuterol Sulfate", page: 1 },
      { id: "psg-12-b1", type: "meta", text: "May 2026", page: 1 },
      { id: "psg-12-b2", type: "p", text: "Active Ingredient: Albuterol sulfate", page: 1 },
      { id: "psg-12-b3", type: "h2", text: "Recommended Studies:", page: 1 },
      {
        id: "psg-12-b4",
        type: "p",
        text: "Three in vitro bioequivalence studies are recommended for this product.",
        page: 2,
      },
    ],
    ...over,
  };
}

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

    // The head is a label plus a count chip, not one string.
    expect(screen.getByText("Repository")).toBeInTheDocument();
    expect(screen.getByText("7 docs")).toBeInTheDocument();
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

/**
 * Select `needle` inside the block that contains it, the way a drag does.
 * DocumentCanvas listens on document "selectionchange" and reads the live
 * selection, so a real Range is the only way to reach the selection toolbar.
 */
function selectText(needle: string): void {
  const block = [...document.querySelectorAll<HTMLElement>("[data-block-id]")].find((el) =>
    (el.textContent ?? "").includes(needle),
  );
  if (!block) throw new Error(`no block contains "${needle}"`);
  const node = [...block.childNodes].find(
    (n): n is Text => n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").includes(needle),
  );
  if (!node) throw new Error(`"${needle}" is not in a bare text node`);
  const start = (node.textContent ?? "").indexOf(needle);
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, start + needle.length);
  // jsdom implements Range without layout; the canvas positions the toolbar
  // from the range's rect, so it needs one.
  range.getBoundingClientRect = () => ({
    top: 100,
    left: 40,
    width: 60,
    height: 16,
    bottom: 116,
    right: 100,
    x: 40,
    y: 100,
    toJSON: () => ({}),
  });
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  fireEvent(document, new Event("selectionchange"));
}

describe("Reference library", () => {
  async function openAlbuterol() {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    fetchPsgContentMock.mockResolvedValue(psgContent());
    render(<StudioPage />);
    await userEvent.click(await screen.findByRole("button", { name: /^A - / }));
    await userEvent.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));
    await screen.findByText("Guidance on Albuterol Sulfate");
  }

  async function openAlbuterolPdf() {
    await openAlbuterol();
    await userEvent.click(screen.getByRole("button", { name: "View original PDF" }));
  }

  it("lists the database PSGs grouped by letter, drug and form", async () => {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    render(<StudioPage />);

    expect(
      await screen.findByRole("heading", { name: /Reference library/ }),
    ).toBeInTheDocument();
    // Same shape as the repository header: label, then a count chip.
    expect(screen.getByText("3 PSGs")).toBeInTheDocument();
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
    // The head is a label plus a count chip, not one string.
    expect(screen.getByText("Repository")).toBeInTheDocument();
    expect(screen.getByText("7 docs")).toBeInTheDocument();
  });

  it("opens a PSG as a read-only document with the same chrome a draft gets", async () => {
    await openAlbuterol();

    // The PSG's own text, on the same canvas a working document uses.
    expect(screen.getByText("Recommended Studies:")).toBeInTheDocument();
    expect(screen.getByText(/Three in vitro bioequivalence studies/)).toBeInTheDocument();
    expect(screen.getByText("Read-only - FDA reference")).toBeInTheDocument();
    // Read-only means no editing surface at all, not a textbox that refuses
    // keystrokes: no block is contentEditable and none announces as a textbox.
    expect(document.querySelector('[data-block-id][contenteditable="true"]')).toBeNull();
    expect(screen.queryAllByRole("textbox", { name: /Paragraph \d+ of/ })).toHaveLength(0);
    // It carries FDA's date, never an invented internal version number.
    expect(screen.getByText("PSG_020503 Albuterol Sulfate.docx")).toBeInTheDocument();
    expect(document.querySelector(".st-foot__v")?.textContent).toBe("2024-05-01");
    // The chrome a working document gets, a PSG gets: rail, panels, check.
    expect(screen.getByRole("button", { name: /^Compliance results/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check this document" })).toBeEnabled();
    expect(
      screen.getByText(/Reads what this guidance requires of an application/),
    ).toBeInTheDocument();
    // The repository-wide sweep still belongs to the working documents.
    expect(screen.getByRole("button", { name: /^Check all/ })).toBeDisabled();
    const link = screen.getByRole("link", { name: "Open on fda.gov" });
    expect(link).toHaveAttribute(
      "href",
      "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf",
    );
  });

  it("checks a PSG against what the guidance actually requires", async () => {
    fetchPsgRequirementsMock.mockResolvedValue(psgRequirements());
    await openAlbuterol();

    await userEvent.click(screen.getByRole("button", { name: "Check this document" }));

    // Real extracted requirements, anchored to the FDA words they came from.
    expect(await screen.findByText("Recommended study")).toBeInTheDocument();
    expect(screen.getByText("Study design")).toBeInTheDocument();
    const marks = [...document.querySelectorAll("[data-finding-id]")].map(
      (el) => el.textContent,
    );
    expect(marks).toContain("Recommended Studies:");
    expect(marks).toContain("Three in vitro bioequivalence studies");
    expect(fetchPsgRequirementsMock).toHaveBeenCalledWith(12);
  });

  it("drops a requirement whose quote is not in the rendered text", async () => {
    fetchPsgRequirementsMock.mockResolvedValue(
      psgRequirements({
        requirements: [
          {
            key: "dissolution",
            label: "Dissolution test",
            value: "USP <711>",
            page: 3,
            quote: "a sentence this document does not contain",
          },
        ],
      }),
    );
    await openAlbuterol();
    await userEvent.click(screen.getByRole("button", { name: "Check this document" }));

    // Anchoring it to a guess would highlight the wrong sentence of an FDA
    // guidance, so it is dropped rather than shown.
    expect(
      await screen.findByRole("button", { name: /^Compliance results/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Dissolution test")).not.toBeInTheDocument();
  });

  it("answers questions about a PSG from the real guidance service", async () => {
    askQueryMock.mockResolvedValue({
      answer: "A waiver may be requested under 21 CFR 320.22(b)(1).",
      citations: [{ short_name: "Albuterol Sulfate", page: 2 }],
    } as QueryAnswer);
    await openAlbuterol();

    await userEvent.click(screen.getByRole("button", { name: /^Ask/ }));
    await userEvent.type(
      screen.getByRole("textbox", { name: /Ask about this document/i }),
      "What study is recommended?",
    );
    await userEvent.keyboard("{Enter}");

    expect(await screen.findByText(/A waiver may be requested/)).toBeInTheDocument();
    // Scoped to this PSG's drug, so the answer cannot come from another one.
    expect(askQueryMock).toHaveBeenCalledWith(
      "What study is recommended?",
      expect.objectContaining({ normalized_name: "albuterol sulfate" }),
    );
    expect(screen.getByText("Albuterol Sulfate - page 2")).toBeInTheDocument();
  });

  it("offers the PSG as a .docx download from the same-origin path", async () => {
    await openAlbuterol();

    const download = screen.getByRole("link", { name: "Download .docx" });
    expect(download).toHaveAttribute("href", "/api/psg/documents/12/docx");
    expect(download).toHaveAttribute("download");
  });

  it("switches between the extracted text and the original PDF", async () => {
    await openAlbuterol();
    expect(screen.queryByTitle(/PSG PDF/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "View original PDF" }));
    const frame = await screen.findByTitle(/PSG PDF: Albuterol Sulfate/);
    expect(frame).toHaveAttribute("src", "/api/psg/documents/12/pdf");
    expect(screen.queryByText("Recommended Studies:")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "View text" }));
    expect(screen.getByText("Recommended Studies:")).toBeInTheDocument();
    expect(screen.queryByTitle(/PSG PDF/)).not.toBeInTheDocument();
  });

  it("recovers when the PSG text cannot be loaded", async () => {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    fetchPsgContentMock.mockRejectedValueOnce(new Error("boom"));
    render(<StudioPage />);
    await userEvent.click(await screen.findByRole("button", { name: /^A - / }));
    await userEvent.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));

    const fallback = await screen.findByRole("alert");
    expect(fallback).toHaveTextContent(/Couldn't load the text of this PSG/);
    // A failure never renders as an empty document.
    expect(screen.queryByText("Guidance on Albuterol Sulfate")).not.toBeInTheDocument();

    fetchPsgContentMock.mockResolvedValue(psgContent());
    await userEvent.click(within(fallback).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Guidance on Albuterol Sulfate")).toBeInTheDocument();
  });

  it("says so when the rebuilt text is incomplete", async () => {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    fetchPsgContentMock.mockResolvedValue(psgContent({ truncated: true }));
    render(<StudioPage />);
    await userEvent.click(await screen.findByRole("button", { name: /^A - / }));
    await userEvent.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));

    expect(await screen.findByText(/Extract incomplete/)).toBeInTheDocument();
  });

  it("never resurrects a PSG the analyst closed before its text arrived", async () => {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    let release: (c: PsgDocumentContent) => void = () => {};
    fetchPsgContentMock.mockImplementationOnce(
      () => new Promise<PsgDocumentContent>((resolve) => (release = resolve)),
    );
    render(<StudioPage />);
    await userEvent.click(await screen.findByRole("button", { name: /^A - / }));
    await userEvent.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));

    // Back to the draft before the reply lands.
    await userEvent.keyboard("{Escape}");
    expect(screen.getByText(/3\.2\.S\.4\.1 Specification - Drug Substance/)).toBeInTheDocument();

    await act(async () => {
      release(psgContent());
    });

    // The draft is still the open document, and it still behaves like one.
    // A resurrected reference doc is invisible on the canvas but takes over
    // the selection model: it hides the draft's assistant actions and reroutes
    // its highlights to a document nobody has open.
    expect(screen.queryByText("Guidance on Albuterol Sulfate")).not.toBeInTheDocument();
    expect(screen.getByText(/3\.2\.S\.4\.1 Specification - Drug Substance/)).toBeInTheDocument();

    selectText("Assay");
    expect(await screen.findByRole("toolbar", { name: /selected text/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explain" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Highlight/ }));
    expect(document.querySelector("mark.st-mark--highlight")).not.toBeNull();
  });

  it("offers every selection action on a reference PSG", async () => {
    await openAlbuterol();

    selectText("Recommended Studies:");
    const toolbar = await screen.findByRole("toolbar", { name: /selected text/ });
    // Scoped to the toolbar: "Check" also names the footer's check button.
    for (const action of ["Highlight", "Summarize", "Explain", "Check", "Ask"]) {
      expect(within(toolbar).getByRole("button", { name: action })).toBeInTheDocument();
    }

    // Highlighting is local to the text, and lands on the PSG.
    await userEvent.click(screen.getByRole("button", { name: /Highlight/ }));
    const mark = document.querySelector("mark.st-mark--highlight");
    expect(mark?.textContent).toBe("Recommended Studies:");
  });

  it("sends a selection action on a PSG to the real guidance service", async () => {
    askQueryMock.mockResolvedValue({
      answer: "Three in vitro studies are recommended.",
      citations: [],
    } as unknown as QueryAnswer);
    await openAlbuterol();

    selectText("Recommended Studies:");
    await userEvent.click(await screen.findByRole("button", { name: /Explain/ }));

    expect(await screen.findByText(/Three in vitro studies are recommended/)).toBeInTheDocument();
    expect(askQueryMock).toHaveBeenCalledWith(
      expect.stringContaining("Recommended Studies:"),
      expect.objectContaining({ normalized_name: "albuterol sulfate" }),
    );
  });

  it("ignores a slow reply for a PSG the analyst has left", async () => {
    fetchPsgLibraryMock.mockResolvedValue(LIB_ROWS);
    let releaseFirst: (c: PsgDocumentContent) => void = () => {};
    fetchPsgContentMock.mockImplementationOnce(
      () => new Promise<PsgDocumentContent>((resolve) => (releaseFirst = resolve)),
    );
    fetchPsgContentMock.mockResolvedValue(
      psgContent({ id: 13, blocks: [{ id: "psg-13-b0", type: "title", text: "Guidance on Albuterol", page: 1 }] }),
    );
    render(<StudioPage />);
    await userEvent.click(await screen.findByRole("button", { name: /^A - / }));

    await userEvent.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));
    await userEvent.click(screen.getByRole("button", { name: /Tablet \(Oral\)/ }));
    await screen.findByText("Guidance on Albuterol");

    // The first document's text arrives late; it must not land under the
    // second document's header.
    await act(async () => {
      releaseFirst(psgContent());
    });
    expect(screen.getByText("Guidance on Albuterol")).toBeInTheDocument();
    expect(screen.queryByText("Guidance on Albuterol Sulfate")).not.toBeInTheDocument();
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
    fetchPsgContentMock.mockResolvedValue(psgContent());
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
    await screen.findByText("Guidance on Albuterol Sulfate");
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
    // The fda.gov link lives on the reference bar, which outlives both panes,
    // so the fallback does not carry a second copy of it.
    expect(screen.getByRole("link", { name: "Open on fda.gov" })).toBeInTheDocument();

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
    fetchPsgContentMock.mockResolvedValue(psgContent());
    render(<StudioPage />);

    // Start a check on the draft, then open a PSG before it completes.
    await user.click(screen.getByRole("button", { name: "Check this document" }));
    await user.click(await screen.findByRole("button", { name: /^A - / }));
    await user.click(screen.getByRole("button", { name: /Aerosol, Metered \(Inhalation\)/ }));
    await screen.findByText("Guidance on Albuterol Sulfate");

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    // The completed check must not put an invisible panel's scrim over the PSG.
    expect(screen.getByText("Guidance on Albuterol Sulfate")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close panel" })).not.toBeInTheDocument();
  });
});
