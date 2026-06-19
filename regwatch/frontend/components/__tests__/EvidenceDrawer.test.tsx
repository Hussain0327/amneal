import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EvidenceDrawer } from "@/components/EvidenceDrawer";
import type { Citation } from "@/lib/api";

afterEach(cleanup);

const CITE: Citation = {
  short_name: "Albuterol PSG",
  page: 3,
  chunk_id: "ch_1",
  doc_id: 1,
  version_id: 1,
  source_url: "https://www.accessdata.fda.gov/albuterol.pdf",
  snippet: "Conduct a single-dose, randomized, crossover BE study under fasting conditions.",
};

describe("EvidenceDrawer", () => {
  it("renders nothing when closed (citation = null)", () => {
    render(<EvidenceDrawer citation={null} onClose={() => {}} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the snippet, source line and source link when open", () => {
    render(<EvidenceDrawer citation={CITE} onClose={() => {}} />);

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(CITE.snippet)).toBeInTheDocument();
    expect(screen.getByText(/Albuterol PSG · p\.3/)).toBeInTheDocument();

    const link = screen.getByRole("link", { name: /Open source PDF/ });
    expect(link).toHaveAttribute("href", CITE.source_url);
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("moves focus to the close button on open", () => {
    render(<EvidenceDrawer citation={CITE} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: /Close evidence/ })).toHaveFocus();
  });

  it("returns focus to the opener when closed", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();

    const { rerender } = render(<EvidenceDrawer citation={CITE} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: /Close evidence/ })).toHaveFocus();

    rerender(<EvidenceDrawer citation={null} onClose={() => {}} />);
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    render(<EvidenceDrawer citation={CITE} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on backdrop (scrim) click", () => {
    const onClose = vi.fn();
    render(<EvidenceDrawer citation={CITE} onClose={onClose} />);
    const scrim = document.querySelector(".evidence__scrim");
    expect(scrim).not.toBeNull();
    fireEvent.click(scrim as Element);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
