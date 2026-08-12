// The record drawer's vocabulary: what an artifact is made of, what was asked
// of it, and what the corpus behind it holds.
//
// PURE. Rendered turns and wire rows in, view models out -- no I/O, no clock,
// no randomness -- so every rule below is unit-testable without a DOM. The
// three panels that render these are the only things here that fetch.
//
// ONE NUMBERING, ONE PAIRING. Two facts in this file are already owned
// elsewhere and are read rather than recomputed: an authority's [n] comes from
// authoritiesFrom (the same call the margin makes, so the drawer's [3] and the
// prose's [3] cannot disagree), and a turn's identity comes from turnKey (the
// same string the transcript renders as a key, so an entry can point at the
// turn it describes). Re-deriving either would create a second version of a
// fact that has to stay single.

import type { Citation, PsgLibraryDoc } from "./api";
import { citeKey } from "./citations";
import { authoritiesFrom, type Authority } from "./research-types";
import { nonAnswerLabel, type Turn } from "./turns";

/**
 * FDA's own two words for a guidance's standing.
 *
 * Absent stays absent -- see `narrowPsgType`. A citation that carries no
 * psg_type is a citation whose standing we were not told, which is a different
 * claim from "draft".
 */
export type PsgType = "draft" | "final";

/**
 * The statuses that may carry grounding.
 *
 * INV-2: a refusal, a clarify or a conversational turn renders no citation
 * surface in the transcript, so nothing in this drawer may hand one back. The
 * same gate `latestAuthorities` applies on the sheet.
 */
const SOURCED_STATUSES: readonly string[] = ["answer", "summary"];

/**
 * An authority as the drawer files it: the margin's entry plus the two fields
 * an 11rem margin has no room for.
 *
 * Both extras are copied off the validated citation, never computed, for the
 * same reason every field on `Authority` is (INV-1).
 */
export interface FiledAuthority extends Authority {
  /** The fda.gov URL, straight off the wire. Guard with safeHref at render. */
  readonly sourceUrl: string | null;
  /** null when the wire did not say -- NOT "draft". See `PsgType`. */
  readonly psgType: PsgType | null;
}

/** One answered turn's sources, filed under the question that produced them. */
export interface RecordFiling {
  /** The transcript key of the assistant turn, so the entry can point at it. */
  readonly key: string;
  /** The question verbatim. "" when no question precedes the turn, which a
   * rehydrated transcript with a dropped row can produce. */
  readonly question: string;
  /** ISO 8601 off the turn, or null. Formatted at render, never here. */
  readonly askedAt: string | null;
  readonly authorities: readonly FiledAuthority[];
}

/** How an answered turn settled, in the vocabulary the analyst reads. */
export type HistoryTone = "answer" | "clarify" | "declined" | "plain";

/** One turn of the artifact's audit trail. */
export interface HistoryEntry {
  readonly key: string;
  readonly question: string;
  readonly askedAt: string | null;
  /** The settled outcome as a phrase, e.g. "Answered", "Evidence gap". */
  readonly outcome: string;
  /** Which vocabulary `outcome` belongs to, so the panel can mark it without
   * re-deriving the verdict and reaching a second opinion. */
  readonly tone: HistoryTone;
  /** Sources the prose actually stamped. 0 on every unsourceable status. */
  readonly sourceCount: number;
  readonly modelName: string | null;
  readonly auditId: number | null;
  /** The stream died mid-draft and the answer was re-fetched. */
  readonly fellBack: boolean;
  /** The server's own withdrawal signal, never inferred. */
  readonly draftWithdrawn: string | null;
}

/** What the assistant can see of the product on the sheet. */
export interface StudioScope {
  /** The backend's canonical drug key -- the `normalized_name` filter value. */
  readonly normalizedName: string;
  readonly dosageForm: string | null;
}

/** One catalog row as the corpus search lists it. */
export interface CorpusHit {
  readonly id: number;
  readonly ingredient: string;
  /** "Aerosol, Metered (Inhalation)", degrading to whichever side exists. */
  readonly form: string;
  readonly psgType: PsgType;
  readonly recommendedDate: string | null;
  readonly sourceUrl: string | null;
}

/** A bounded page of corpus matches, with the true size of the match set. */
export interface CorpusResult {
  readonly hits: readonly CorpusHit[];
  /**
   * How many rows matched, which is NOT `hits.length` once the cap bites.
   * Carried separately so a capped list can say so: a truncated list rendered
   * as the whole answer is the same lie an unreachable list rendered as empty
   * would be.
   */
  readonly matched: number;
}

/**
 * The identity of a turn, and therefore the anchor the drawer points at.
 *
 * Live turns carry meta.turn_id, rehydrated ones the server row id, and only
 * then the index -- exactly the order the transcript uses. Exported so the
 * transcript and the drawer read ONE function rather than two copies of a
 * string template that could drift by a hyphen.
 */
export function turnKey(turn: Turn, index: number): string {
  return `${turn.role}-${turn.meta?.turn_id ?? turn.id ?? index}`;
}

/** The DOM id the transcript hangs on a turn so the drawer can scroll to it. */
export function turnAnchorId(key: string): string {
  return `rw-turn-${key}`;
}

/** "Aerosol, Metered (Inhalation)", degrading to whichever side exists. */
function formLabel(dosageForm: string | null, route: string | null): string {
  const form = (dosageForm ?? "").trim();
  const via = (route ?? "").trim();
  if (form && via) return `${form} (${via})`;
  return form || via || "Form not stated";
}

/** "final" only when the wire says so; every other value is the weaker claim,
 * and an absent one is no claim at all. */
function narrowPsgType(raw: string | null | undefined): PsgType | null {
  if (raw === undefined || raw === null || raw.trim() === "") return null;
  return raw === "final" ? "final" : "draft";
}

/** Same rule, for a catalog row that always states one. */
function catalogPsgType(raw: string | null | undefined): PsgType {
  return raw === "final" ? "final" : "draft";
}

function isSourced(turn: Turn): boolean {
  return turn.status !== null && SOURCED_STATUSES.includes(turn.status);
}

/**
 * This turn's authorities, with the source URL and standing joined back on.
 *
 * The join is by citeKey against the RAW citation array, which is the same key
 * `authoritiesFrom` deduplicates on -- so every authority finds the citation it
 * was made from, and a repeated source resolves to the first occurrence, which
 * is the one the prose numbered.
 */
function fileCitations(citations: readonly Citation[]): readonly FiledAuthority[] {
  const byKey = new Map<string, Citation>();
  for (const c of citations) {
    const key = citeKey(c.short_name, c.page);
    if (!byKey.has(key)) byKey.set(key, c);
  }
  return authoritiesFrom(citations).map((authority: Authority): FiledAuthority => {
    const source = byKey.get(citeKey(authority.shortName, authority.page));
    return {
      ...authority,
      sourceUrl: source?.source_url ?? null,
      psgType: narrowPsgType(source?.psg_type),
    };
  });
}

/**
 * Every source the artifact stands on, filed under the question that fetched it.
 *
 * TRANSCRIPT ORDER, oldest first, and deliberately not newest-first. The
 * drawer's rule runs down one spine beside a sheet that reads top-down; two
 * orders in one room is the thing that would need explaining. What is on the
 * sheet NOW already has its own view -- the authorities margin -- so the drawer
 * is free to be the whole thread instead of a second copy of the latest turn.
 *
 * A turn with no citations files nothing rather than filing an empty group: an
 * empty group under a question states that sources exist and are merely out of
 * sight, which is the one thing this product must never imply.
 */
export function fileAuthorities(turns: readonly Turn[]): readonly RecordFiling[] {
  const filings: RecordFiling[] = [];
  let question = "";
  turns.forEach((turn, index) => {
    if (turn.role === "user") {
      question = turn.content;
      return;
    }
    if (!isSourced(turn) || turn.citations.length === 0) return;
    const authorities = fileCitations(turn.citations);
    if (authorities.length === 0) return;
    filings.push({
      key: turnKey(turn, index),
      question,
      askedAt: turn.createdAt,
      authorities,
    });
  });
  return filings;
}

/**
 * How many DISTINCT sources a set of filings stands on.
 *
 * Deduplicated by chunk id across filings, not summed: one guidance passage
 * cited by four answers is one thing the artifact stands on, and reporting it
 * as four overstates the breadth of the record behind the work.
 */
export function countSources(filings: readonly RecordFiling[]): number {
  const seen = new Set<string>();
  for (const filing of filings) {
    for (const authority of filing.authorities) seen.add(authority.chunkId);
  }
  return seen.size;
}

/** The settled outcome and its vocabulary, read off one turn. */
function outcomeOf(turn: Turn): { outcome: string; tone: HistoryTone } {
  const declined = nonAnswerLabel(turn.status, turn.refused, turn.reason);
  if (declined !== null) return { outcome: declined, tone: "declined" };
  // A noun phrase, like every other outcome here: these name the state a turn
  // settled INTO, and "asked" would be the only verb in the column.
  if (turn.status === "clarify") return { outcome: "Clarification", tone: "clarify" };
  // A conversational turn makes no claim about the record, so it is neither
  // answered nor declined and owes no sources. #193 split it out of the
  // refusal vocabulary for exactly this reason.
  if (turn.status === "meta") return { outcome: "Conversational", tone: "plain" };
  if (turn.status === "summary") return { outcome: "Summarised", tone: "answer" };
  return { outcome: "Answered", tone: "answer" };
}

/**
 * The artifact's audit trail: every turn, how it settled, and what settled it.
 *
 * Unlike `fileAuthorities` this keeps the turns that produced nothing --
 * a refusal and an error are the entries an analyst reconstructing a filing
 * most needs to find, and a trail that silently drops them is not a trail.
 *
 * Same transcript order, for the same reason.
 */
export function fileHistory(turns: readonly Turn[]): readonly HistoryEntry[] {
  const entries: HistoryEntry[] = [];
  let question = "";
  turns.forEach((turn, index) => {
    if (turn.role === "user") {
      question = turn.content;
      return;
    }
    const { outcome, tone } = outcomeOf(turn);
    entries.push({
      key: turnKey(turn, index),
      question,
      askedAt: turn.createdAt,
      outcome,
      tone,
      // Counted off the same deduped list the prose stamps, and only on a
      // status that may carry grounding, so the trail and the sheet agree.
      sourceCount: isSourced(turn) ? authoritiesFrom(turn.citations).length : 0,
      // Empty is absent. turnFromMessage builds meta from a persisted audit_id
      // and fills model_name with "" because history does not store it, so a
      // rehydrated turn would otherwise render an empty model beside a real
      // audit id -- a provenance line that names nothing.
      modelName: (turn.meta?.model_name ?? "").trim() || null,
      auditId: turn.meta?.audit_id ?? null,
      fellBack: turn.streamFellBack,
      draftWithdrawn: turn.draftWithdrawn,
    });
  });
  return entries;
}

/** The most frequent non-empty value, ties broken by first appearance. */
function commonest(values: readonly (string | null | undefined)[]): string | null {
  const counts = new Map<string, number>();
  for (const raw of values) {
    const value = (raw ?? "").trim();
    if (value === "") continue;
    counts.set(value, (counts.get(value) ?? 0) + 1);
  }
  let best: string | null = null;
  let bestCount = 0;
  // Map iteration is insertion-ordered, so a strict > keeps the first-seen
  // value on a tie and the derived scope is deterministic across renders.
  for (const [value, count] of counts) {
    if (count > bestCount) {
      best = value;
      bestCount = count;
    }
  }
  return best;
}

/**
 * The product the artifact is about, as far as its own sources can say.
 *
 * Read off the NEWEST sourced turn, not the whole thread: a thread that moved
 * from one ingredient to another is about the second one, and scoping a new
 * question to the first would silently answer about the wrong drug.
 *
 * `product_name` is the backend's `normalized_name` (turn_gate.py writes it
 * from the passage's own key), so it is exactly the value the
 * `normalized_name` query filter expects -- no re-normalizing here, which
 * would be a second normalizer to keep true.
 *
 * Returns null when no sourced turn exists, which the assistant states rather
 * than papering over with the raw question text.
 */
export function studioScope(turns: readonly Turn[]): StudioScope | null {
  const last = turns.findLast((turn) => turn.role === "assistant" && isSourced(turn));
  if (last === undefined || last.citations.length === 0) return null;
  const normalizedName = commonest(last.citations.map((c) => c.product_name));
  if (normalizedName === null) return null;
  // The form is read only from the citations that share the product, so a
  // stray passage about another drug cannot contribute its dosage form.
  const forms = last.citations
    .filter((c) => (c.product_name ?? "").trim() === normalizedName)
    .map((c) => c.dosage_form);
  return { normalizedName, dosageForm: commonest(forms) };
}

/** Everything about a catalog row a query may match against. */
function haystack(doc: PsgLibraryDoc): string {
  return [
    doc.active_ingredient,
    doc.stripped_name,
    doc.normalized_name,
    doc.dosage_form ?? "",
    doc.route ?? "",
    doc.appl_no ?? "",
  ]
    .join(" ")
    .toLowerCase();
}

/**
 * The corpus rows matching a query.
 *
 * AND over whitespace-separated terms, because this is a filter and not a
 * search ranker: "albuterol aerosol" must narrow to the aerosol guidances
 * rather than widen to everything mentioning either word. Substring rather
 * than prefix, so "sulfate" finds "Albuterol Sulfate".
 *
 * An empty query matches NOTHING rather than everything. The panel does not
 * fetch until it is asked, and a blank field resolving to all 1,795 rows would
 * render a wall the analyst never asked for.
 */
export function searchCorpus(
  docs: readonly PsgLibraryDoc[],
  query: string,
  limit: number,
): CorpusResult {
  const terms = query.toLowerCase().split(/\s+/).filter((t) => t !== "");
  if (terms.length === 0) return { hits: [], matched: 0 };
  const hits: CorpusHit[] = [];
  let matched = 0;
  for (const doc of docs) {
    const hay = haystack(doc);
    if (!terms.every((term) => hay.includes(term))) continue;
    matched += 1;
    // Counting continues past the cap so `matched` is the true size of the
    // match set and the panel can say what it is not showing.
    if (hits.length >= limit) continue;
    hits.push({
      id: doc.id,
      ingredient: doc.active_ingredient,
      form: formLabel(doc.dosage_form, doc.route),
      psgType: catalogPsgType(doc.psg_type),
      recommendedDate: doc.recommended_date,
      sourceUrl: doc.source_url,
    });
  }
  return { hits, matched };
}
