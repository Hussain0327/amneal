// The unified work list behind the Research Studio's work rail: the four
// artifact kinds, fanned out to the list endpoints that already exist.
//
// DELIBERATELY CLIENT-SIDE, AND DELIBERATELY NOT A NEW /work ENDPOINT.
// A server-side /work would have to decide today what a work item is for all
// four kinds, what order they come back in, and who can see whose -- and we do
// not know any of that is right yet. Three of the four kinds still render
// through their old surfaces, and the rail has not been used in anger once.
// Inventing the endpoint now is exactly the speculative abstraction the house
// rules forbid, and it is the expensive kind to unpick: a route, a contract
// test, a shape other clients start depending on. Four GETs and a merge in the
// browser cost nothing to delete the day the shape is known.

import { listSessions, listWhitepaperRuns, watchLatest } from "@/lib/api";
import type { ArtifactKind, KindGroup, WorkItem } from "@/lib/research-types";
import { relTime } from "@/lib/time";

/** Rail order, top to bottom. Also the fan-out order, so the two cannot drift. */
export const KIND_ORDER: readonly ArtifactKind[] = ["thread", "dossier", "bulletin", "paper"];

/** The plural the rail heads each group with. */
export const KIND_LABEL: Record<ArtifactKind, string> = {
  thread: "Threads",
  dossier: "Dossiers",
  bulletin: "Bulletins",
  paper: "Papers",
};

/**
 * How long the rail waits on one kind.
 *
 * Far shorter than lib/api.ts's 30s default, and that difference is the point:
 * this is furniture, not an answer. An analyst waiting on a list of their own
 * work has already been failed by a rail that sits blank for half a minute,
 * even if the response eventually lands. When the bound fires the kind resolves
 * to "unreachable" with a retry, which is a better thing to look at than a
 * spinner with no end.
 *
 * What this bounds is honestly the WAIT, not the work: listSessions and friends
 * take no AbortSignal (getJSON in lib/api.ts does not thread one through), so
 * the underlying fetch keeps running and its late answer is simply dropped. A
 * bounded wait with a defined outcome is the guarantee; a cancelled request is
 * not, and claiming otherwise would be a lie in a comment.
 */
const KIND_TIMEOUT_MS = 8_000;

/**
 * White-paper runs are org-shared and unbounded over time, so the rail takes a
 * page rather than the whole ledger. Newest-activity-first is the server's own
 * order, so a page is the recent work, which is what a rail is for.
 */
const PAPER_PAGE_LIMIT = 50;

interface Settled<T> {
  readonly ok: true;
  readonly value: T;
}

interface Unsettled {
  readonly ok: false;
}

type Outcome<T> = Settled<T> | Unsettled;

/**
 * One kind's list, and how many exist behind it.
 *
 * The two are not the same number and the rail must not conflate them: a page
 * of 50 white-paper runs out of 214 is not "50 papers". Where an endpoint pages
 * (/whitepaper/runs, /watch/latest) `total` is the server's own count; where it
 * does not, `total` is the length, because then the length IS the total.
 */
interface KindPage {
  readonly items: readonly WorkItem[];
  readonly total: number;
}

/**
 * The server's count, floored at the page it actually sent.
 *
 * `total` and the rows come from two queries, so a count racing an insert can
 * come back SMALLER than the list beside it -- and a malformed payload can omit
 * it entirely, which the compile-time type does not catch (getJSON casts). Left
 * alone, the rail would head a visibly non-empty group with a smaller number, or
 * with 0, or with "undefined": a false number set in the same type as a true
 * one, which is the exact failure the count exists to prevent. The one thing
 * always true is "at least this many", so the page is the floor. Validated here,
 * at the boundary, rather than defended again at every reader.
 */
function atLeast(total: number, items: readonly WorkItem[]): number {
  return Number.isFinite(total) ? Math.max(total, items.length) : items.length;
}

/**
 * Wait on one kind's fetch under the bound, and turn every way it can go wrong
 * into the same answer.
 *
 * Three things lose the race and all three mean "we have no list": the timer,
 * the caller's unmount signal, and a rejection from the call itself. Collapsing
 * them is deliberate -- the rail says the same sentence for all three, and a
 * discriminated failure nobody reads is state nobody maintains.
 *
 * Both the timer and the abort listener are released on every path, including
 * the successful one, so a rail that reloads on every visit does not accrete
 * listeners on a long-lived AbortSignal.
 */
async function bounded<T>(work: Promise<T>, signal: AbortSignal): Promise<Outcome<T>> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;
  try {
    const value = await Promise.race([
      work,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error("work list timed out")), KIND_TIMEOUT_MS);
        onAbort = () => reject(new Error("work list abandoned"));
        if (signal.aborted) onAbort();
        else signal.addEventListener("abort", onAbort, { once: true });
      }),
    ]);
    return { ok: true, value };
  } catch {
    return { ok: false };
  } finally {
    clearTimeout(timer);
    if (onAbort) signal.removeEventListener("abort", onAbort);
  }
}

async function threadItems(): Promise<KindPage> {
  const data = await listSessions();
  const items = data.sessions.map((s) => ({
    id: s.id,
    kind: "thread" as const,
    // The server names a session asynchronously, so an untitled row is a real
    // state and not a bug. Name it rather than render a blank row.
    title: s.title.trim() || "Untitled thread",
    updatedAt: relTime(s.updated_at),
  }));
  // /sessions takes no limit and returns the caller's own threads whole, so
  // there is no page to be short of. Recompute this the day it paginates.
  return { items, total: items.length };
}

async function bulletinItems(): Promise<KindPage> {
  const data = await watchLatest();
  const items = data.alerts.map((a) => ({
    // A bulletin is a document AT a version -- the same PSG revised twice is
    // two bulletins -- so the identity is the pair, not the document.
    id: `${a.psg_document_id}:${a.psg_version_id}`,
    kind: "bulletin" as const,
    title: a.active_ingredient.trim() || `Application ${a.listing_appl_no}`,
    updatedAt: relTime(a.captured_at),
  }));
  // /watch/latest bounds its own page and reports the count behind it.
  return { items, total: atLeast(data.total, items) };
}

async function paperItems(): Promise<KindPage> {
  const data = await listWhitepaperRuns({ limit: PAPER_PAGE_LIMIT });
  const items = data.runs.map((r) => ({
    id: String(r.id),
    kind: "paper" as const,
    // Ingredient first, because that is what an analyst calls the paper. The
    // application number is the last resort and never a blank row.
    title: r.ingredient.trim() || r.rld_name_input.trim() || `Application ${r.application_number}`,
    updatedAt: relTime(r.updated_at),
  }));
  return { items, total: atLeast(data.total, items) };
}

function itemsFor(kind: Exclude<ArtifactKind, "dossier">): Promise<KindPage> {
  switch (kind) {
    case "thread":
      return threadItems();
    case "bulletin":
      return bulletinItems();
    case "paper":
      return paperItems();
  }
}

/** True for the four kinds, so a `?kind=` from the URL can be trusted. */
export function isArtifactKind(value: string | null): value is ArtifactKind {
  return value !== null && (KIND_ORDER as readonly string[]).includes(value);
}

/** Every kind in "loading", for the first paint before any fetch settles. */
export function loadingGroups(): readonly KindGroup[] {
  return KIND_ORDER.map(
    (kind): KindGroup => ({
      kind,
      label: KIND_LABEL[kind],
      items: [],
      total: 0,
      state: "loading",
    }),
  );
}

/**
 * One kind's group. Never rejects: a failure IS an answer here ("unreachable"),
 * and a rejecting leg would take the other three down with it in the fan-out.
 *
 * Exported on its own because the rail's Retry acts on one kind: re-fetching
 * all four to recover one would throw away three lists that are already good.
 */
export async function fetchKindGroup(
  kind: ArtifactKind,
  signal: AbortSignal,
): Promise<KindGroup> {
  const label = KIND_LABEL[kind];
  if (kind === "dossier") {
    // Dossiers have no list because the backend keeps none: POST /assemble
    // composes a dossier out of the corpus, returns it, and writes nothing. So
    // this resolves READY AND EMPTY, which is the true statement -- "you have
    // no saved dossiers" -- and pointedly not "unreachable", which would claim
    // a request failed when none was made. The day /assemble persists, this
    // becomes a fetch like the other three and nothing else here changes.
    return { kind, label, items: [], total: 0, state: "ready" };
  }
  const outcome = await bounded(itemsFor(kind), signal);
  // items stays [] and total 0 on the failure path because the type needs both,
  // NOT because the list is empty or the count is zero. Consumers read `state`
  // first (WorkRail does).
  return outcome.ok
    ? { kind, label, items: outcome.value.items, total: outcome.value.total, state: "ready" }
    : { kind, label, items: [], total: 0, state: "unreachable" };
}

/**
 * All four groups, in rail order.
 *
 * allSettled semantics, arrived at by making every leg unrejectable rather than
 * by unwrapping PromiseSettledResult: fetchKindGroup absorbs its own failure
 * into `state`, so Promise.all here cannot be short-circuited and one dead
 * endpoint cannot blank the other three. A single await chain -- the obvious
 * first draft -- would have done exactly that.
 *
 * Never rejects, for the same reason.
 */
export function fetchWorkGroups(signal: AbortSignal): Promise<readonly KindGroup[]> {
  return Promise.all(KIND_ORDER.map((kind) => fetchKindGroup(kind, signal)));
}
