// The Research Studio's shared vocabulary.
//
// Five types the shell, the two rails and the sheet pass between each other,
// and one mapper. Nothing about an artifact's CONTENT is redeclared here: that
// lives in lib/api.ts's generated wire types, and a hand-written second copy
// would be a second thing to keep true.

import type { Citation } from "@/lib/api";
import { citationIndex, citeKey, dedupeCitations } from "@/lib/citations";

/**
 * The four kinds of thing this studio produces.
 *
 * Nouns, not verbs. "Ask / Assemble / Watch / White Paper" named four commands
 * because the surfaces read as chapters of one sequence; these name four things
 * an analyst owns, which is what the work rail actually lists.
 */
export type ArtifactKind = "thread" | "dossier" | "bulletin" | "paper";

/**
 * What the work rail knows about one kind's list.
 *
 * "unreachable" is a third state on purpose. "You have no dossiers" and "we
 * could not ask" are different sentences, and only one of them is an invitation
 * to make one -- a failed fetch that renders as an empty list tells the analyst
 * something false and they stop looking.
 */
export type KindState = "ready" | "loading" | "unreachable";

/**
 * One cited source, as it hangs in the authorities margin.
 *
 * The margin is the record's own hand, so every field is copied off a validated
 * citation and none is computed: the snippet shown is the snippet the backend
 * matched (INV-1).
 */
export interface Authority {
  /** 1-based, and the SAME number the [n] stamp carries in the prose. */
  readonly n: number;
  readonly shortName: string;
  readonly page: number;
  readonly recommendedDate: string | null;
  readonly snippet: string;
  readonly chunkId: string;
}

/** One row in the work rail. */
export interface WorkItem {
  readonly id: string;
  readonly kind: ArtifactKind;
  readonly title: string;
  /**
   * Already formatted for display, not an ISO string: the rail renders this
   * verbatim, and formatting it there would mean reading the clock during
   * render. lib/research-work.ts stamps it with relTime() at fetch time.
   */
  readonly updatedAt: string;
}

/** One kind's section of the work rail. */
export interface KindGroup {
  readonly kind: ArtifactKind;
  /** "Threads" | "Dossiers" | "Bulletins" | "Papers". */
  readonly label: string;
  /**
   * Empty whenever `state` is not "ready" -- the type needs an array, but only
   * a ready group's emptiness means "none". Read `state` first.
   */
  readonly items: readonly WorkItem[];
  readonly state: KindState;
}

/** The three panels behind the record rail. */
export type RecordPanelId = "record" | "assistant" | "history";

/**
 * A turn's citations, as the authorities that hang in its margin.
 *
 * ONE mapping in ONE place, because the margin's [n] and the prose's [n] have
 * to be the same number. Markdown.tsx numbers a stamp by position in
 * `dedupeCitations(turn.citations)`, so the margin is numbered off that exact
 * list rather than off the raw wire array -- a duplicated wire citation would
 * otherwise leave the margin one ahead of the prose, and [3] in the text would
 * light the wrong source.
 *
 * The only transformation applied is `?? null` on the optional revision date:
 * the margin states "no revision date recorded" rather than inventing one, and
 * a single null is easier to state than undefined-or-null.
 */
export function authoritiesFrom(citations: readonly Citation[]): readonly Authority[] {
  // The margin's [n] MUST be the prose's [n]. Markdown.tsx numbers a stamp with
  // citationIndex, which counts position in the RAW array and lets the first
  // occurrence win, so a repeated source leaves gaps: [A, A, B] numbers B as 3.
  // Numbering the deduped array instead would call that same B "2" and every
  // stamp past the first duplicate would point at the wrong authority. One
  // index, read by both sides.
  const index = citationIndex([...citations]);
  return dedupeCitations(citations).map((c) => ({
    n: index.get(citeKey(c.short_name, c.page)) ?? 0,
    shortName: c.short_name,
    page: c.page,
    recommendedDate: c.recommended_date ?? null,
    snippet: c.snippet,
    chunkId: c.chunk_id,
  }));
}
