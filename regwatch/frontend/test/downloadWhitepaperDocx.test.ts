// The docx download helper hands a server-suggested Content-Disposition
// filename to anchor.download before appending the anchor to the DOM. These
// tests pin the sanitizer between that remote header and the DOM: path
// separators and control characters must never reach the download attribute.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { downloadWhitepaperDocx } from "@/lib/api";

const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

function docxResponse(disposition: string | null): Response {
  const headers = new Headers({ "content-type": DOCX_MIME });
  if (disposition !== null) headers.set("content-disposition", disposition);
  return new Response(new Blob(["docx-bytes"], { type: DOCX_MIME }), { status: 200, headers });
}

describe("downloadWhitepaperDocx filename handling", () => {
  let anchors: HTMLAnchorElement[];

  beforeEach(() => {
    anchors = [];
    const create = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation(
      (tagName: string, options?: ElementCreationOptions): HTMLElement => {
        const el = create(tagName, options);
        if (tagName === "a") anchors.push(el as HTMLAnchorElement);
        return el;
      },
    );
    // jsdom implements neither object-URL call; the helper needs stable stubs.
    URL.createObjectURL = vi.fn(() => "blob:stub");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("strips path separators and control chars from a hostile filename", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(docxResponse("attachment; filename*=UTF-8''..%2F..%2Fpasswd%00.docx")),
    );
    await downloadWhitepaperDocx(7, "020503");
    expect(anchors).toHaveLength(1);
    expect(anchors[0].download).toBe(".._.._passwd.docx");
  });

  it("falls back to the run-derived name when the header is absent", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(docxResponse(null)));
    await downloadWhitepaperDocx(7, "020503");
    expect(anchors).toHaveLength(1);
    expect(anchors[0].download).toBe("whitepaper_020503.docx");
  });
});
