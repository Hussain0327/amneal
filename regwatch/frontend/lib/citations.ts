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

/**
 * One citation per case-insensitive (short_name, page) key, first occurrence
 * wins -- the SAME dedupe rule and order citationIndex numbers by. Every surface
 * that displays or resolves a [n] (stamp index, stamp-resolution array, chips,
 * references) must consume the same deduped list, or a duplicated wire citation
 * makes [n] point at the wrong source.
 */
export function dedupeCitations<T extends MatchableCitation>(citations: readonly T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const c of citations) {
    const key = citeKey(c.short_name, c.page);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

// The trailing "Sources:" bibliography. Mirrors the backend's _SOURCES_TRAILER
// (src/regwatch/common/citations.py): everything after a line reading
// "Sources:" is the model-authored source list. The UI renders its own
// references from the VALIDATED citations array, so the trailer is redundant
// display text -- prose-only consumers drop it, exactly as the backend's
// strip_sources_trailer does.
const SOURCES_TRAILER = /\n\s*Sources:\s*\n/;

export interface SplitAnswer {
  prose: string;
  trailer: string | null;
}

/**
 * Splits an answer into prose and its trailing "Sources:" bibliography (null
 * when absent). A degenerate answer that is ONLY a bibliography keeps its full
 * text as prose -- display dedupe must never blank a reply.
 */
export function splitSourcesTrailer(text: string): SplitAnswer {
  const m = SOURCES_TRAILER.exec(text);
  if (!m) return { prose: text, trailer: null };
  const prose = text.slice(0, m.index);
  if (!prose.trim()) return { prose: text, trailer: null };
  return { prose, trailer: text.slice(m.index + m[0].length) };
}

// One numbered trailer line: an optional bracketed marker, then the entry
// ("[1] [PSG_020503, p.4]" / "1. PSG_020503, p.4"). The entry text is handed
// to the SAME pair grammar the backend validated, so a marker resolves only
// through a source-shaped (short_name, page) pair.
const TRAILER_LINE = /^\s*\[?(\d{1,3})[\].):]*\s+(.+)$/;

/**
 * Maps the model's own bibliography numbering (the bare [n] markers it cites
 * with inline) to the (short_name, page) pair each trailer line names. This is
 * the model's statement about its own text, never a guess: a marker with no
 * parseable trailer pair stays unmapped, and the renderer still resolves every
 * mapped pair against the turn's VALIDATED citations before a stamp renders
 * (INV-1 -- an unmatched marker remains literal prose).
 */
export function trailerMarkerPairs(trailer: string): Map<number, CitePair> {
  const map = new Map<number, CitePair>();
  for (const line of trailer.split("\n")) {
    const m = TRAILER_LINE.exec(line);
    if (!m) continue;
    const pairs = pairsIn(m[2]);
    if (pairs.length === 0) continue;
    const n = Number(m[1]);
    if (!map.has(n)) map.set(n, pairs[0]);
  }
  return map;
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

// ---------------------------------------------------------------------------
// Human-identifying labels
// ---------------------------------------------------------------------------

// What a citation needs to name itself to a person. short_name ("PSG_020911")
// is an FDA application number and names nothing a reader can act on, so it
// becomes the FALLBACK: what we show when a turn predates identity fields on
// the wire. It stays visible in the reference row and drawer as the identifier
// support conversations use.
export interface LabelableCitation extends MatchableCitation {
  readonly product_name?: string | null;
  readonly dosage_form?: string | null;
  readonly route?: string | null;
  readonly psg_type?: string | null;
  readonly recommended_date?: string | null;
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/** Title-case an FDA listing string ("AEROSOL, METERED" -> "Aerosol, Metered"). */
function titleCase(value: string): string {
  return value
    .toLowerCase()
    .replace(/(^|[\s,/-])([a-z])/g, (_m, sep: string, ch: string) => sep + ch.toUpperCase());
}

/**
 * The dosage form phrase: route then form, e.g. "Inhalation Aerosol, Metered".
 *
 * Collapses to one term when either string already contains the other, so a
 * route of "ORAL" against a form of "TABLET, ORAL" does not stutter. Never
 * drops a qualifier to shorten the label — "AEROSOL, METERED" is a different
 * product from "AEROSOL", and provenance that rounds off is provenance that
 * misidentifies.
 */
export function formPhrase(
  route: string | null | undefined,
  form: string | null | undefined,
): string | null {
  const r = route?.trim() ? titleCase(route.trim()) : null;
  const f = form?.trim() ? titleCase(form.trim()) : null;
  if (!r) return f;
  if (!f) return r;
  const rl = r.toLowerCase();
  const fl = f.toLowerCase();
  if (fl.includes(rl)) return f;
  if (rl.includes(fl)) return r;
  return `${r} ${f}`;
}

/** "2021-03-15" -> "Mar 2021". Null for anything unparseable or absent. */
export function revisedMonth(value: string | null | undefined): string | null {
  if (!value) return null;
  const m = /^(\d{4})-(\d{2})/.exec(value.trim());
  if (!m) return null;
  const month = Number(m[2]);
  if (!Number.isInteger(month) || month < 1 || month > 12) return null;
  return `${MONTHS[month - 1]} ${m[1]}`;
}

/** The product line alone: "Beclomethasone Dipropionate — Inhalation Aerosol". */
export function citationProduct(c: LabelableCitation): string | null {
  const product = c.product_name?.trim() ? titleCase(c.product_name.trim()) : null;
  if (!product) return null;
  const form = formPhrase(c.route, c.dosage_form);
  return form ? `${product} — ${form}` : product;
}

/**
 * The always-visible chip label:
 * "Beclomethasone Dipropionate — Inhalation Aerosol PSG, revised Mar 2021 · p.1"
 *
 * Falls back whole to "PSG_020911 · p.1" when the citation carries no product
 * name — the legacy case, where inventing an identity would be worse than
 * showing the opaque one.
 */
export function citationLabel(c: LabelableCitation): string {
  const product = citationProduct(c);
  if (!product) return `${c.short_name} · p.${c.page}`;
  const revised = revisedMonth(c.recommended_date);
  const head = revised ? `${product} PSG, revised ${revised}` : `${product} PSG`;
  return `${head} · p.${c.page}`;
}

/**
 * Chip labels for a whole turn, disambiguated.
 *
 * Two PSGs for the same ingredient and form collapse to the same human label
 * (audit #1716 cited PSG_020911 and PSG_207921 for one product), which would
 * show the reader two identical chips pointing at different documents. When
 * that happens BOTH gain their application number; a label that is already
 * unique never carries one.
 */
export function citationLabels(citations: readonly LabelableCitation[]): string[] {
  const labels = citations.map(citationLabel);
  const counts = new Map<string, number>();
  for (const label of labels) counts.set(label, (counts.get(label) ?? 0) + 1);
  return labels.map((label, i) => {
    if ((counts.get(label) ?? 0) < 2) return label;
    const applNo = citations[i].short_name.replace(/^(PSG_|OB_)/i, "");
    return `${label} · #${applNo}`;
  });
}
