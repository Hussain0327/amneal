import { describe, expect, it } from "vitest";

import { syncTextareaHeight } from "@/lib/composer";

// jsdom does not lay out content, so scrollHeight is faked per-element. The
// tests pin the handler's contract (reset-to-auto then lock-to-scrollHeight),
// which is what makes the composer grow AND shrink with content.
function textareaWithScrollHeight(px: number, record?: string[]): HTMLTextAreaElement {
  const el = document.createElement("textarea");
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    get() {
      record?.push(el.style.height); // capture the height at measure time
      return px;
    },
  });
  return el;
}

describe("syncTextareaHeight (composer auto-grow)", () => {
  it("locks height to scrollHeight so the composer grows with content", () => {
    const el = textareaWithScrollHeight(120);
    syncTextareaHeight(el);
    expect(el.style.height).toBe("120px");
  });

  it("resets to auto before measuring so it can also shrink", () => {
    const seen: string[] = [];
    const el = textareaWithScrollHeight(40, seen);
    el.style.height = "120px"; // a previously grown height
    syncTextareaHeight(el);
    expect(seen[0]).toBe("auto"); // measured AFTER the reset, not the stale 120px
    expect(el.style.height).toBe("40px"); // shrank to the new content height
  });

  it("is a no-op on a null ref", () => {
    expect(() => syncTextareaHeight(null)).not.toThrow();
  });
});
