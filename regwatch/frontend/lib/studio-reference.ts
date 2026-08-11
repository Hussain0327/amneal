/**
 * Turns a reference PSG from the API into the same StudioDoc the working
 * documents use, so one canvas renders both. Pure: wire rows in, document
 * out. No I/O, no clock, no randomness.
 *
 * The document arrives with no findings and `checkState: "unchecked"`,
 * because nothing has read it yet. Running the check fills them in from
 * `toReferenceFindings` below -- and those are REQUIREMENTS the guidance
 * places on an applicant, not defects in the guidance. A PSG has no defects
 * for us to find; saying otherwise about an FDA document would be the one
 * unforgivable thing this surface could do.
 */

import type { PsgDocumentContent, PsgRequirement } from "./api";
import type { Block, BlockType, Finding, StudioDoc } from "./studio-types";

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

/**
 * Turns the requirements ingest extracted from a PSG into anchored findings.
 *
 * A finding in this surface is a span of the document, so a requirement only
 * becomes one when its extractor quote can still be located in the rendered
 * text. One that cannot is dropped rather than anchored to a guess: a
 * highlight over the wrong sentence of an FDA guidance is worse than no
 * highlight. `unanchored` reports how many were dropped, so the panel can say
 * so instead of quietly showing fewer.
 *
 * Severity is always "info". These are not defects; they are what the
 * guidance asks of an applicant.
 */
export function toReferenceFindings(
  requirements: readonly PsgRequirement[],
  blocks: readonly Block[],
): { findings: Finding[]; unanchored: number } {
  const findings: Finding[] = [];
  let unanchored = 0;

  for (const requirement of requirements) {
    const anchor = requirement.quote ? locate(requirement.quote, blocks) : null;
    if (!anchor) {
      unanchored += 1;
      continue;
    }
    findings.push({
      id: `psg-req-${requirement.key}`,
      severity: "info",
      title: requirement.label,
      detail: requirement.value,
      blockId: anchor.blockId,
      start: anchor.start,
      end: anchor.end,
      excerpt: anchor.excerpt,
      location: requirement.page === null ? "This guidance" : `Page ${requirement.page}`,
      standard: "FDA product-specific guidance",
    });
  }

  return { findings, unanchored };
}

/**
 * Where a quote sits in the rebuilt text, or null.
 *
 * Exact match first. Failing that, whitespace is normalised on both sides,
 * because the extractor read the PDF's own line wrapping while the rebuild
 * rejoined those lines: the words agree, the spaces do not. Block text has no
 * runs of whitespace left (the server collapses them), so an offset found in
 * the normalised copy is the offset in the real text.
 */
function locate(
  quote: string,
  blocks: readonly Block[],
): { blockId: string; start: number; end: number; excerpt: string } | null {
  const needle = quote.trim();
  if (!needle) return null;

  for (const block of blocks) {
    const exact = block.text.indexOf(needle);
    if (exact >= 0) {
      return {
        blockId: block.id,
        start: exact,
        end: exact + needle.length,
        excerpt: block.text.slice(exact, exact + needle.length),
      };
    }
  }

  const flat = needle.replace(/\s+/g, " ");
  for (const block of blocks) {
    const found = block.text.replace(/\s+/g, " ").indexOf(flat);
    if (found < 0) continue;
    return {
      blockId: block.id,
      start: found,
      end: found + flat.length,
      excerpt: block.text.slice(found, found + flat.length),
    };
  }
  return null;
}
