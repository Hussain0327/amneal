// Frontend mirror of the backend citation grammar (src/regwatch/common/citations.py).
//
// The answer markdown carries LITERAL inline citation tags written by the
// generator, e.g. "[PSG_020503, p.3]" or the compound "[A, p.1; B, p.2]". This
// module parses those tags with the SAME grammar the backend validates against,
// so a tag the backend accepted is the tag the UI stamps — the two can never
// drift on what counts as a citation. INV-1: only a (short_name, page) pair that
// matches a real citation on the turn becomes a stamp; everything else stays
// literal prose.

// One source token inside a bracket: short_name + page, e.g. "PSG_020503, p.4".
// Kept source-shaped (>=3 trailing digits, optional PSG_/OB_ prefix) so prose
// like "[Table 1, p.3]" is NOT treated as a citation. Mirrors `_PAIR`.
const PAIR = /((?:PSG_|OB_)?\d{3,})\s*,\s*p\.\s*(\d+)/gi;

// Any bracketed run with no nested brackets. A bracket is treated as a citation
// only if it contains >=1 PAIR, so prose like "[see appendix]" is left alone.
// Mirrors `_BRACKET`.
const BRACKET = /\[([^[\]]+)\]/g;

export interface CitePair {
  shortName: string;
  page: number;
}

// Every (short_name, page) pair inside one bracket body (compound-aware).
export function pairsIn(body: string): CitePair[] {
  const out: CitePair[] = [];
  // Fresh lastIndex per call: PAIR is a module-level /g regex.
  PAIR.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = PAIR.exec(body)) !== null) {
    out.push({ shortName: m[1], page: Number(m[2]) });
  }
  return out;
}

// A segment of an answer text node: either plain prose, or a citation bracket
// carrying its parsed pairs (and the exact source text, so an unmatched bracket
// can be re-emitted verbatim).
export type CiteSegment =
  | { kind: "text"; value: string }
  | { kind: "cite"; raw: string; pairs: CitePair[] };

// Split a single text node into prose / citation-bracket segments. Brackets with
// no PAIR stay as plain text (folded into the surrounding prose). The caller
// decides per-pair whether a bracket renders as stamps or literal text (INV-1).
export function segmentCitations(text: string): CiteSegment[] {
  const segments: CiteSegment[] = [];
  let last = 0;
  BRACKET.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = BRACKET.exec(text)) !== null) {
    const pairs = pairsIn(m[1]);
    if (pairs.length === 0) continue; // not a citation — leave inside prose
    if (m.index > last) segments.push({ kind: "text", value: text.slice(last, m.index) });
    segments.push({ kind: "cite", raw: m[0], pairs });
    last = m.index + m[0].length;
  }
  if (last < text.length) segments.push({ kind: "text", value: text.slice(last) });
  return segments;
}

export interface MatchableCitation {
  short_name: string;
  page: number;
}

// Case-insensitive key for (short_name, page), matching the backend's IGNORECASE
// citation parser: the model may echo a bracket lowercase while the canonical
// short_name is uppercase. A case-sensitive miss would drop a valid stamp.
export function citeKey(shortName: string, page: number): string {
  return `${shortName.toUpperCase()}:${page}`;
}

// Build the lookup the renderer matches tags against: key -> 1-based index into
// the turn's citation list (the [n] the stamp displays and the citation it opens).
export function citationIndex(citations: MatchableCitation[]): Map<string, number> {
  const index = new Map<string, number>();
  citations.forEach((c, i) => {
    const key = citeKey(c.short_name, c.page);
    // First occurrence wins: the backend de-dupes citations in order of
    // appearance, so the first index is the canonical [n] for that source.
    if (!index.has(key)) index.set(key, i + 1);
  });
  return index;
}
