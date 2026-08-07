import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EvidenceDrawer } from "@/components/EvidenceDrawer";
import type { Citation } from "@/lib/api";

// The drawer is pure presentation over one already-validated citation; these
// tests pin the provenance additions: the guidance revision (version_id,
// type-guarded because legacy rehydrated citations are passthrough dicts), the
// explicit "not recorded" recency state, and the labeled PDF link.

function citation(overrides: Partial<Citation>): Citation {
  return {
    short_name: "PSG_020503",
    page: 3,
    chunk_id: "PSG_020503-3",
    doc_id: 1,
    version_id: 4,
    source_url: "https://example.test/doc.pdf",
    snippet: "snippet text",
    ...overrides,
  };
}

describe("EvidenceDrawer -- source provenance", () => {
  it("portals into document.body and shows the revision + recency", () => {
    render(
      <EvidenceDrawer
        citation={citation({ recommended_date: "Jan 2026", diff_summary: "BE table updated" })}
        onClose={vi.fn()}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: /PSG_020503, page 3/ });
    // Rendered through a portal: the drawer's root hangs off document.body,
    // outside the test container.
    expect(dialog.closest(".evidence")?.parentElement).toBe(document.body);
    expect(dialog.querySelector(".evidence__src")?.textContent).toContain("v4");
    expect(dialog.textContent).toContain("FDA recommended");
    expect(dialog.textContent).toContain("Jan 2026");
    // With a date present, the explicit empty state must not also render.
    expect(dialog.textContent).not.toContain("Revision date not recorded");
  });

  it("omits the revision for a legacy passthrough citation without version_id", () => {
    // Legacy rehydrated citations are passthrough dicts -- version_id may be
    // missing despite the generated type marking it required.
    const legacy = { ...citation({}), version_id: undefined } as unknown as Citation;
    render(<EvidenceDrawer citation={legacy} onClose={vi.fn()} />);
    const src = screen.getByRole("dialog").querySelector(".evidence__src");
    expect(src?.textContent).not.toContain("v4");
    expect(src?.textContent).toContain("PSG_020503");
  });

  it("states 'Revision date not recorded' when no recency rode the wire", () => {
    render(<EvidenceDrawer citation={citation({})} onClose={vi.fn()} />);
    const dialog = screen.getByRole("dialog");
    const none = dialog.querySelector(".recency .recency__none");
    expect(none?.textContent).toBe("Revision date not recorded");
  });

  it("labels the source link 'Open source PDF' with the citation's href", () => {
    render(<EvidenceDrawer citation={citation({})} onClose={vi.fn()} />);
    // The decorative arrow is aria-hidden, so the accessible name is exactly
    // the label.
    const link = screen.getByRole("link", { name: "Open source PDF" });
    expect(link).toHaveAttribute("href", "https://example.test/doc.pdf");
  });
});
