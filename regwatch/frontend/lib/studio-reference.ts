/**
 * Turns a reference PSG from the API into the same StudioDoc the working
 * documents use, so one canvas renders both. Pure: wire rows in, document
 * out. No I/O, no clock, no randomness.
 *
 * What this deliberately does NOT do is invent compliance state. The document
 * comes back with no findings and `checkState: "unchecked"`, because nothing
 * has checked it -- and nothing can: a PSG is FDA's published guidance, not a
 * controlled record of ours to hold findings against. An empty findings list
 * here is the truth, not a placeholder.
 */

import type { PsgDocumentContent } from "./api";
import type { Block, BlockType, StudioDoc } from "./studio-types";

/** The block types the API can send. Anything else degrades to a paragraph. */
const BLOCK_TYPES: readonly BlockType[] = ["title", "meta", "h2", "p"];

function narrowBlockType(raw: string): BlockType {
  return (BLOCK_TYPES as readonly string[]).includes(raw) ? (raw as BlockType) : "p";
}

/** "Solution (Subcutaneous)", degrading to whichever side the row has. */
export function formLabel(content: PsgDocumentContent): string {
  const form = (content.dosage_form ?? "").trim();
  const route = (content.route ?? "").trim();
  if (form && route) return `${form} (${route})`;
  return form || route || "Form not stated";
}

/**
 * The reference document's id. Matches the rail's LibraryDoc id ("psg-{n}")
 * so the tree's selected row and the open document are the same identity, and
 * so it can never collide with a fixture id.
 */
export function referenceDocId(psgId: number): string {
  return `psg-${psgId}`;
}

/**
 * Builds the StudioDoc for one reference PSG.
 *
 * `version` carries FDA's recommended date rather than a version number: a
 * PSG has no version of ours, and printing "v1" beside a document we did not
 * author would invent an internal history it does not have.
 */
export function toReferenceDoc(content: PsgDocumentContent): StudioDoc {
  const blocks: Block[] = content.blocks.map((block) => ({
    id: block.id,
    type: narrowBlockType(block.type),
    text: block.text,
    marks: [],
  }));

  return {
    id: referenceDocId(content.id),
    name: content.file_name,
    path: "Reference library",
    version: content.recommended_date ?? "not dated",
    blocks,
    findings: [],
    checkState: "unchecked",
    // The guidance IS the standard here; it is not measured against one.
    standards: [],
  };
}
