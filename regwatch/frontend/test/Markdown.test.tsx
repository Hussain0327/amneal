import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Markdown } from "@/components/Markdown";
import type { Citation } from "@/lib/api";

function cite(short_name: string, page: number): Citation {
  return {
    short_name,
    page,
    chunk_id: `${short_name}-${page}`,
    doc_id: 1,
    version_id: 1,
    source_url: "https://example.test/doc.pdf",
    snippet: "snippet text",
  };
}

describe("Markdown citation stamps", () => {
  it("turns a matched [short_name, p.N] tag into a clickable stamp wired to onCite", async () => {
    const onCite = vi.fn();
    const c = cite("PSG_020503", 3);
    render(
      <Markdown citations={[c]} onCite={onCite}>
        {"A BE study is recommended [PSG_020503, p.3]."}
      </Markdown>,
    );
    const stamp = screen.getByRole("button", { name: /Source 1: PSG_020503, page 3/i });
    expect(stamp).toHaveTextContent("[1]");
    expect(stamp).toHaveClass("cite-stamp");
    await userEvent.click(stamp);
    // INV-1: the click opens the exact citation that backed the matched tag.
    expect(onCite).toHaveBeenCalledTimes(1);
    expect(onCite).toHaveBeenCalledWith(c);
  });

  it("stamps a tag the model echoed in lowercase (backend validates case-insensitively)", async () => {
    // The backend's citation validator is IGNORECASE, so a lowercase-echoed
    // bracket is a real, backend-blessed input. The stamp must resolve via the
    // 1-based data-n index (same array the remark index was built from), not a
    // case-sensitive name match that would silently drop the anchor.
    const onCite = vi.fn();
    const c = cite("PSG_020503", 3);
    render(
      <Markdown citations={[c]} onCite={onCite}>
        {"A BE study is recommended [psg_020503, p.3]."}
      </Markdown>,
    );
    const stamp = screen.getByRole("button", { name: /Source 1: PSG_020503, page 3/i });
    expect(stamp).toHaveTextContent("[1]");
    await userEvent.click(stamp);
    expect(onCite).toHaveBeenCalledWith(c);
  });

  it("renders an unmatched tag as literal prose, never a stamp (INV-1)", () => {
    const onCite = vi.fn();
    const { container } = render(
      <Markdown citations={[cite("PSG_020503", 3)]} onCite={onCite}>
        {"This claim cites [PSG_999999, p.9] which is not in the citation list."}
      </Markdown>,
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(container).toHaveTextContent("[PSG_999999, p.9]");
    expect(onCite).not.toHaveBeenCalled();
  });

  it("leaves a tag inside a code span untouched", () => {
    const onCite = vi.fn();
    const { container } = render(
      <Markdown citations={[cite("PSG_020503", 3)]} onCite={onCite}>
        {"Inline code: `[PSG_020503, p.3]` stays literal."}
      </Markdown>,
    );
    // No stamp button; the literal tag survives inside a <code> element.
    expect(screen.queryByRole("button")).toBeNull();
    const code = container.querySelector("code");
    expect(code).not.toBeNull();
    expect(code).toHaveTextContent("[PSG_020503, p.3]");
  });

  it("leaves a tag inside a fenced code block untouched", () => {
    const onCite = vi.fn();
    const { container } = render(
      <Markdown citations={[cite("PSG_020503", 3)]} onCite={onCite}>
        {"```\n[PSG_020503, p.3]\n```"}
      </Markdown>,
    );
    expect(screen.queryByRole("button")).toBeNull();
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre).toHaveTextContent("[PSG_020503, p.3]");
  });

  it("renders no stamps and no drawer trigger when citations are omitted (INV-2)", () => {
    const { container } = render(<Markdown>{"Out of scope [PSG_020503, p.3]."}</Markdown>);
    expect(screen.queryByRole("button")).toBeNull();
    // The tag stays literal; nothing clickable is produced.
    expect(container).toHaveTextContent("[PSG_020503, p.3]");
  });

  it("numbers stamps from the deduped list and resolves clicks against it", async () => {
    const onCite = vi.fn();
    const a = cite("PSG_020503", 3);
    const duplicateOfA = cite("PSG_020503", 3);
    const b = cite("PSG_021730", 4);
    render(
      <Markdown citations={[a, duplicateOfA, b]} onCite={onCite}>
        {"First [PSG_020503, p.3], then [PSG_021730, p.4]."}
      </Markdown>,
    );
    // The duplicate must not leave a numbering hole: B is [2], never [3].
    expect(screen.getByRole("button", { name: /Source 1: PSG_020503, page 3/i })).toHaveTextContent(
      "[1]",
    );
    const stampB = screen.getByRole("button", { name: /Source 2: PSG_021730, page 4/i });
    expect(stampB).toHaveTextContent("[2]");
    await userEvent.click(stampB);
    // The stamp resolution array must be the SAME deduped list the index was
    // built from -- deduping only the index would resolve [2] to A's duplicate
    // (raw citations[1]) and open the wrong source (INV-1).
    expect(onCite).toHaveBeenCalledTimes(1);
    expect(onCite).toHaveBeenCalledWith(b);
  });

  it("stamps each source of a compound tag and keeps a link working alongside", async () => {
    const onCite = vi.fn();
    const a = cite("PSG_020503", 4);
    const b = cite("PSG_021730", 4);
    render(
      <Markdown citations={[a, b]} onCite={onCite}>
        {"See [docs](https://fda.test) and the rule [PSG_020503, p.4; PSG_021730, p.4]."}
      </Markdown>,
    );
    expect(screen.getByRole("button", { name: /Source 1: PSG_020503, page 4/i })).toHaveTextContent("[1]");
    expect(screen.getByRole("button", { name: /Source 2: PSG_021730, page 4/i })).toHaveTextContent("[2]");
    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://fda.test");
    await userEvent.click(screen.getByRole("button", { name: /Source 2/i }));
    expect(onCite).toHaveBeenCalledWith(b);
  });
});
