// Research Studio behaviour tests.
//
// The studio shipped with a green build and nothing that executed it. These
// cover the four claims the surface is actually built on -- the stamp/authority
// link in both directions, the unsourced verdict, the third list state, and the
// one numbering both columns have to agree on -- plus the four functional bugs
// the audit found, each pinned by a case that fails without its fix.
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { CitationStamp } from "@/components/CitationStamp";
import { Sheet } from "@/components/research/Sheet";
import { kindHeadLabel, WorkRail } from "@/components/research/WorkRail";
import type { Citation } from "@/lib/api";
import {
  artifactTitle,
  authoritiesFrom,
  type ArtifactKind,
  type Authority,
  type KindGroup,
  type KindState,
  type WorkItem,
} from "@/lib/research-types";
import { citationIndex, citeKey } from "@/lib/citations";

function citation(shortName: string, page: number): Citation {
  return {
    chunk_id: `${shortName}-${page}`,
    doc_id: 1,
    page,
    short_name: shortName,
    snippet: `${shortName} p.${page} snippet`,
    source_url: "https://example.test/psg",
    version_id: 1,
  };
}

function item(id: string, kind: ArtifactKind, title: string): WorkItem {
  return { id, kind, title, updatedAt: "2h ago" };
}

function group(
  kind: ArtifactKind,
  label: string,
  state: KindState,
  items: readonly WorkItem[] = [],
  total = items.length,
): KindGroup {
  return { kind, label, state, items, total };
}

/** The sheet is controlled, so a harness owns the one fact both columns read. */
function SheetHarness({
  authorities,
  settled = true,
  children,
}: {
  readonly authorities: readonly Authority[];
  readonly settled?: boolean;
  readonly children?: ReactNode;
}): React.JSX.Element {
  const [litN, setLitN] = useState<number | null>(null);
  return (
    <div className="rw-studio">
      <Sheet
        kicker="Thread"
        title="Albuterol sulfate"
        authorities={authorities}
        litN={litN}
        onLit={setLitN}
        settled={settled}
        footer={null}
      >
        {children}
      </Sheet>
    </div>
  );
}

/** Every rule the sheet has generated for the lit stamp, concatenated. */
function generatedRules(): string {
  return Array.from(document.querySelectorAll("style"))
    .map((s) => s.textContent ?? "")
    .join("");
}

// [A, A, B]: the repeat is what separates raw-array numbering from deduped
// numbering, so every numbering assertion below uses this shape.
const REPEATED = [citation("PSG_020503", 3), citation("PSG_020503", 3), citation("PSG_078112", 9)];

describe("a [n] stamp and its authority light each other", () => {
  function renderLinkedSheet(): readonly Authority[] {
    const authorities = authoritiesFrom(REPEATED);
    render(
      <SheetHarness authorities={authorities}>
        <p>
          Grounded <CitationStamp n={1} citation={REPEATED[0]} onCite={vi.fn()} /> and also{" "}
          <CitationStamp n={3} citation={REPEATED[2]} onCite={vi.fn()} />.
        </p>
      </SheetHarness>,
    );
    return authorities;
  }

  it("prose to margin, by pointer", () => {
    renderLinkedSheet();
    const stamp = screen.getByRole("button", { name: /^Source 3:/ });

    fireEvent.mouseOver(stamp);
    expect(screen.getByRole("button", { name: /^Authority 3\./ }).className).toContain("is-lit");

    fireEvent.mouseOut(stamp);
    expect(screen.getByRole("button", { name: /^Authority 3\./ }).className).not.toContain(
      "is-lit",
    );
  });

  it("prose to margin, by keyboard focus", async () => {
    renderLinkedSheet();
    const stamp = screen.getByRole("button", { name: /^Source 3:/ });

    await userEvent.tab();
    await userEvent.tab();
    expect(stamp).toHaveFocus();
    expect(screen.getByRole("button", { name: /^Authority 3\./ }).className).toContain("is-lit");
  });

  it("margin to prose, by pointer -- the rule names the stamp, not its neighbour", () => {
    renderLinkedSheet();
    const entry = screen.getByRole("button", { name: /^Authority 3\./ });

    fireEvent.mouseEnter(entry);
    expect(generatedRules()).toContain('[data-authority-n="3"]');
    expect(generatedRules()).not.toContain('[data-authority-n="2"]');

    fireEvent.mouseLeave(entry);
    expect(generatedRules()).not.toContain("data-authority-n");
  });

  it("margin to prose, by keyboard focus", async () => {
    renderLinkedSheet();
    const entry = screen.getByRole("button", { name: /^Authority 1\./ });

    // Tabbed to, not focused programmatically. entry.focus() does not fire the
    // handler in jsdom and fireEvent.focus() does not move activeElement, so
    // asserting both with those two calls would prove the halves separately and
    // keyboard parity not at all -- it would still pass with the margin taken
    // out of the tab order entirely.
    for (let i = 0; i < 8 && document.activeElement !== entry; i += 1) {
      await userEvent.tab();
    }
    expect(entry).toHaveFocus();
    expect(generatedRules()).toContain('[data-authority-n="1"]');
  });

  it("stamps carry the attribute the sheet delegates on", () => {
    renderLinkedSheet();
    expect(screen.getByRole("button", { name: /^Source 3:/ })).toHaveAttribute(
      "data-authority-n",
      "3",
    );
  });
});

describe("the unsourced turn", () => {
  it("says so, and renders no margin at all -- not an empty one", () => {
    render(
      <SheetHarness authorities={[]}>
        <p>A refusal.</p>
      </SheetHarness>,
    );

    expect(screen.getByText("Not sourced")).toBeInTheDocument();
    expect(document.querySelector(".rs-margin")).toBeNull();
    expect(document.querySelector(".rs-margin__list")).toBeNull();
    // The rule is what promises authorities, so it goes with them.
    expect(document.querySelector(".rs-sheet__grid--unruled")).not.toBeNull();
  });

  it("stays silent while the turn is still arriving", () => {
    render(
      <SheetHarness authorities={[]} settled={false}>
        <p>Streaming...</p>
      </SheetHarness>,
    );

    // "Not sourced" is a verdict about a turn. There is not one yet.
    expect(screen.queryByText("Not sourced")).toBeNull();
    expect(document.querySelector(".rs-margin")).toBeNull();
  });
});

describe("the work rail's third state", () => {
  const noop = vi.fn();

  it("says unavailable, and never says zero", () => {
    render(
      <WorkRail
        groups={[group("dossier", "Dossiers", "unreachable")]}
        activeId={null}
        onSelect={noop}
        onMake={noop}
        onRetry={noop}
      />,
    );

    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(document.querySelector(".rw-work__count")).toBeNull();

    const head = screen.getByRole("button", { name: /Dossiers/ });
    const name = head.getAttribute("aria-label") ?? "";
    expect(name).toContain("unavailable");
    expect(name).not.toMatch(/\b0\b/);

    expect(screen.getByRole("alert")).toHaveTextContent("We could not load your dossiers.");
    expect(screen.getByRole("button", { name: /Retry loading dossiers/ })).toBeInTheDocument();
  });

  it("an empty READY group is an invitation instead", () => {
    render(
      <WorkRail
        groups={[group("dossier", "Dossiers", "ready")]}
        activeId={null}
        onSelect={noop}
        onMake={noop}
        onRetry={noop}
      />,
    );

    expect(screen.queryByText("Unavailable")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(document.querySelector(".rw-work__count")).toHaveTextContent("0");
    expect(screen.getByText(/Build a dossier/)).toBeInTheDocument();
  });

  it("loading shows neither a count nor a verdict", () => {
    render(
      <WorkRail
        groups={[group("thread", "Threads", "loading")]}
        activeId={null}
        onSelect={noop}
        onMake={noop}
        onRetry={noop}
      />,
    );

    expect(document.querySelector(".rw-work__count")).toBeNull();
    expect(screen.queryByText("Unavailable")).toBeNull();
    expect(screen.getByRole("button", { name: /Threads/ })).toHaveAttribute(
      "aria-label",
      "Threads, loading",
    );
  });
});

describe("the work rail's count", () => {
  const noop = vi.fn();

  it("states the total, not the length of the page it was handed", () => {
    const page = Array.from({ length: 50 }, (_, i) => item(`p-${i}`, "paper", `Paper ${i}`));
    render(
      <WorkRail
        groups={[group("paper", "Papers", "ready", page, 214)]}
        activeId="p-0"
        onSelect={noop}
        onMake={noop}
        onRetry={noop}
      />,
    );

    expect(document.querySelector(".rw-work__count")).toHaveTextContent("214");
    expect(screen.getByText(/Showing 50 of 214/)).toBeInTheDocument();
  });

  it("says nothing about paging when the list is whole", () => {
    const all = [item("p-0", "paper", "Paper 0")];
    render(
      <WorkRail
        groups={[group("paper", "Papers", "ready", all)]}
        activeId="p-0"
        onSelect={noop}
        onMake={noop}
        onRetry={noop}
      />,
    );

    expect(document.querySelector(".rw-work__count")).toHaveTextContent("1");
    expect(screen.queryByText(/Showing/)).toBeNull();
  });
});

describe("the work rail opens the group you are in", () => {
  const noop = vi.fn();

  it("opens it when the kind resolves AFTER the id -- a deep link, or a first send", () => {
    const { rerender } = render(
      <WorkRail
        groups={[group("thread", "Threads", "loading")]}
        activeId="t-1"
        onSelect={noop}
        onMake={noop}
        onRetry={noop}
      />,
    );

    // Nothing to open yet: the rail cannot know which group holds t-1.
    expect(screen.getByRole("button", { name: /Threads/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    rerender(
      <WorkRail
        groups={[group("thread", "Threads", "ready", [item("t-1", "thread", "Bioequivalence")])]}
        activeId="t-1"
        onSelect={noop}
        onMake={noop}
        onRetry={noop}
      />,
    );

    expect(screen.getByRole("button", { name: /Threads/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(document.getElementById("rw-work-thread")).not.toHaveAttribute("hidden");
  });

  it("leaves a group the reader collapsed alone", async () => {
    const groups = [group("thread", "Threads", "ready", [item("t-1", "thread", "Bio")])];
    const { rerender } = render(
      <WorkRail groups={groups} activeId="t-1" onSelect={noop} onMake={noop} onRetry={noop} />,
    );

    await userEvent.click(screen.getByRole("button", { name: /Threads/ }));
    expect(screen.getByRole("button", { name: /Threads/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    rerender(
      <WorkRail groups={groups} activeId="t-1" onSelect={noop} onMake={noop} onRetry={noop} />,
    );
    expect(screen.getByRole("button", { name: /Threads/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});

describe("kindHeadLabel keeps the visible label (WCAG 2.5.3)", () => {
  it.each([
    ["ready, none", group("threads" as ArtifactKind, "Threads", "ready")],
    ["ready, one", group("thread", "Threads", "ready", [item("t-1", "thread", "One")])],
    [
      "ready, several",
      group("thread", "Threads", "ready", [
        item("t-1", "thread", "One"),
        item("t-2", "thread", "Two"),
      ]),
    ],
    ["loading", group("thread", "Threads", "loading")],
    ["unreachable", group("thread", "Threads", "unreachable")],
  ])("%s", (_name, g) => {
    // Speech input says what it sees. The visible label is the plural, so the
    // plural has to survive into the accessible name in every state.
    expect(kindHeadLabel(g).toLowerCase()).toContain(g.label.toLowerCase());
  });

  it("carries the paged count honestly", () => {
    const page = [item("p-0", "paper", "Paper 0")];
    expect(kindHeadLabel(group("paper", "Papers", "ready", page, 214))).toBe(
      "Papers, 214, showing 1",
    );
  });
});

describe("prose numbering and margin numbering agree", () => {
  it("a repeated source leaves the margin on the prose's numbers", () => {
    const index = citationIndex([...REPEATED]);
    const authorities = authoritiesFrom(REPEATED);

    // Two distinct sources out of three citations, and the second is [3] --
    // NOT [2] -- because the prose numbers by position in the raw array.
    expect(authorities.map((a) => a.n)).toEqual([1, 3]);
    for (const a of authorities) {
      expect(a.n).toBe(index.get(citeKey(a.shortName, a.page)));
    }
  });

  it("carries the record's own snippet, never a computed one", () => {
    const [first] = authoritiesFrom(REPEATED);
    expect(first.snippet).toBe(REPEATED[0].snippet);
    expect(first.recommendedDate).toBeNull();
  });
});

describe("artifactTitle", () => {
  const paper = item("p-9", "paper", "Ibuprofen 800mg");

  it('names a NEW artifact "New ..." only when there is no id', () => {
    expect(artifactTitle("thread", null, null, null)).toBe("New thread");
    expect(artifactTitle("paper", null, null, null)).toBe("New paper");
  });

  it("never calls an artifact that already exists a new one", () => {
    // The rail has not resolved, or is unreachable, so there is no item to
    // read a title off -- but the id says something is open.
    expect(artifactTitle("thread", "t-1", null, null)).toBe("Untitled thread");
    expect(artifactTitle("paper", "p-9", null, null)).toBe("Untitled paper");
  });

  it("prefers the thread's own title, then the rail's", () => {
    expect(artifactTitle("thread", "t-1", "  Bioequivalence  ", null)).toBe("Bioequivalence");
    expect(artifactTitle("paper", "p-9", "ignored", paper)).toBe("Ibuprofen 800mg");
  });

  it("ignores a thread title on a kind that is not a thread", () => {
    expect(artifactTitle("paper", "p-9", "a thread's title", null)).toBe("Untitled paper");
  });
});
