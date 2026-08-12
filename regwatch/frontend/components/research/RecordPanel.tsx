"use client";

// RECORD: what the artifact is made out of, and the corpus it was drawn from.
//
// Two registers, and the order is the argument. First what THIS artifact stands
// on -- filed under the question that fetched it, so provenance reads as a
// docket and not as a bibliography. Then the corpus behind it, which is the
// same record before anybody asked it anything.
//
// WHY THIS IS NOT THE AUTHORITIES MARGIN AGAIN
// The margin holds the sources of the turn on the sheet NOW, beside the prose
// they support. This holds every source the thread has ever stood on, with the
// question attached and a way back to the turn. One is the page's own margin;
// this is the drawer behind it. They are numbered by the same call, so [3] here
// is [3] out there, within the turn it belongs to.
//
// WHY THE CORPUS IS NOT FETCHED ON OPEN
// /psg/documents has no query parameter, so searching it means holding the
// catalog -- roughly 1,795 rows -- in the browser and filtering there, exactly
// as the Compliance Studio's rail does. Paying for that every time the drawer
// is opened, to serve the analyst who wanted the four rows above it, is the
// wrong trade. It loads on the first search and is filtered locally after that.

import { useCallback, useEffect, useRef, useState } from "react";

import { PanelEmpty, PanelFrame } from "@/components/research/PanelFrame";
import { LinkOutIcon } from "@/components/research/icons";
import { SearchIcon, SendIcon } from "@/components/studio/icons";
import { fetchPsgLibrary, type PsgLibraryDoc } from "@/lib/api";
import {
  searchCorpus,
  type CorpusResult,
  type FiledAuthority,
  type PsgType,
  type RecordFiling,
} from "@/lib/research-record";
import type { ArtifactKind } from "@/lib/research-types";
import { bounded } from "@/lib/research-work";
import { formatClock } from "@/lib/time";
import { safeHref } from "@/lib/url";

/**
 * How long the drawer waits on the catalog.
 *
 * Longer than the work rail's 8s because this is one large body rather than a
 * short list, and shorter than lib/api.ts's 30s default because a search that
 * has not answered in twelve seconds has already failed the person typing.
 * When it fires the search resolves to "unreachable" with a retry, which is a
 * better thing to look at than a field that never answers.
 */
const CORPUS_TIMEOUT_MS = 12_000;

/**
 * How many matches the drawer lists.
 *
 * A 24rem column is not where anybody reads 400 rows, and the true match count
 * is stated beside the list, so the cap narrows the view without ever
 * misreporting the answer.
 */
const CORPUS_LIMIT = 40;

/** Whether the catalog is in hand. Mirrors KindState for the same reason. */
type CorpusState = "idle" | "loading" | "ready" | "unreachable";

/** What the drawer can honestly say when an artifact has filed nothing. */
const NOTHING_FILED: Record<ArtifactKind, { head: string; line: string }> = {
  thread: {
    head: "Nothing filed yet",
    line: "Ask something, and every source the answer stands on is filed here beside the question that fetched it.",
  },
  // The other three still render their old surfaces, so they have no turns to
  // file. Saying "nothing filed" would read as a verdict on the artifact rather
  // than on what is built.
  dossier: {
    head: "Not filed here yet",
    line: "A dossier composes its sources at request time and keeps none. It files them here when it gets a sheet of its own.",
  },
  bulletin: {
    head: "Not filed here yet",
    line: "A bulletin is a change to one guidance, not an argument built from several. It files its own record when it gets a sheet.",
  },
  paper: {
    head: "Not filed here yet",
    line: "A paper keeps its evidence per cell on its own surface. It files here when it gets a sheet.",
  },
};

interface RecordPanelProps {
  readonly kind: ArtifactKind;
  readonly filings: readonly RecordFiling[];
  /** Scroll the transcript to the turn a filing came from. */
  readonly onJump: (key: string) => void;
  readonly onClose: () => void;
}

/** The standing chip. Nothing is drawn when the wire did not state one. */
function StandingChip({ psgType }: { readonly psgType: PsgType | null }): React.JSX.Element | null {
  if (psgType === null) return null;
  return (
    <span className={`rw-standing rw-standing--${psgType}`}>
      {psgType === "final" ? "Final" : "Draft"}
    </span>
  );
}

function AuthorityRow({ authority }: { readonly authority: FiledAuthority }): React.JSX.Element {
  const href = safeHref(authority.sourceUrl);
  return (
    <li className="rw-rec">
      {/* The gutter stamp. Gold, mono and bounded is this system's word for
          "grounded" -- the same vocabulary the inline [n] stamp uses -- so the
          number here reads as the number out in the prose. */}
      <span className="rw-rec__n" aria-hidden="true">
        {authority.n}
      </span>
      <div className="rw-rec__body">
        <p className="rw-rec__name">
          <span className="rw-sr">Authority {authority.n}. </span>
          {authority.shortName}
        </p>
        <p className="rw-rec__meta">
          <span className="rw-rec__page">
            p.{authority.page} &middot;{" "}
            {authority.recommendedDate === null
              ? "rev not recorded"
              : `rev ${authority.recommendedDate}`}
          </span>
          <StandingChip psgType={authority.psgType} />
        </p>
        {/* The record's own words, so the record's face. Clamped in CSS. */}
        <p className="rw-rec__snip">{authority.snippet}</p>
        {href !== undefined && (
          <a className="rw-rec__out" href={href} target="_blank" rel="noopener noreferrer">
            <LinkOutIcon size={11} />
            {/* Names the destination, not the gesture: a list of links all
                called "Open" is unusable read out one after another. */}
            <span>
              {authority.shortName} on fda.gov
              <span className="rw-sr"> (opens in a new tab)</span>
            </span>
          </a>
        )}
      </div>
    </li>
  );
}

function CorpusRow({ hit }: { readonly hit: CorpusResult["hits"][number] }): React.JSX.Element {
  const href = safeHref(hit.sourceUrl);
  return (
    <li className="rw-rec rw-rec--corpus">
      <span className="rw-rec__n rw-rec__n--quiet" aria-hidden="true">
        PSG
      </span>
      <div className="rw-rec__body">
        <p className="rw-rec__name">{hit.ingredient}</p>
        <p className="rw-rec__meta">
          <span className="rw-rec__page">
            {hit.form}
            {hit.recommendedDate !== null && ` · rec ${hit.recommendedDate}`}
          </span>
          <StandingChip psgType={hit.psgType} />
        </p>
        {href !== undefined && (
          <a className="rw-rec__out" href={href} target="_blank" rel="noopener noreferrer">
            <LinkOutIcon size={11} />
            <span>
              {hit.ingredient} on fda.gov
              <span className="rw-sr"> (opens in a new tab)</span>
            </span>
          </a>
        )}
      </div>
    </li>
  );
}

export function RecordPanel({
  kind,
  filings,
  onJump,
  onClose,
}: RecordPanelProps): React.JSX.Element {
  const [query, setQuery] = useState("");
  const [docs, setDocs] = useState<readonly PsgLibraryDoc[] | null>(null);
  const [corpus, setCorpus] = useState<CorpusState>("idle");
  /** Non-null once a search has run, so an empty result can say "no match"
   * rather than being indistinguishable from "you have not searched". */
  const [searched, setSearched] = useState<string | null>(null);

  // Every in-flight catalog fetch, so unmounting mid-load leaves nothing
  // writing into a dead component. Same guarantee the work rail gives, and the
  // same honest half of it: getJSON threads no signal, so what the abort buys
  // is a result that is guaranteed ignored, not a cancelled request.
  const loadRef = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      loadRef.current?.abort();
    },
    [],
  );

  const loadCorpus = useCallback(async (): Promise<readonly PsgLibraryDoc[] | null> => {
    loadRef.current?.abort();
    const controller = new AbortController();
    loadRef.current = controller;
    setCorpus("loading");
    const outcome = await bounded(fetchPsgLibrary(), controller.signal, CORPUS_TIMEOUT_MS);
    if (controller.signal.aborted) return null;
    if (!outcome.ok) {
      setCorpus("unreachable");
      return null;
    }
    setDocs(outcome.value);
    setCorpus("ready");
    return outcome.value;
  }, []);

  const onSearch = useCallback(
    (e: React.FormEvent): void => {
      e.preventDefault();
      const q = query.trim();
      if (q === "") return;
      setSearched(q);
      // Already in hand: filtering is local and instant from here on.
      if (docs !== null) return;
      void loadCorpus();
    },
    [query, docs, loadCorpus],
  );

  const onRetry = useCallback((): void => {
    void loadCorpus();
  }, [loadCorpus]);

  // Live once the catalog is loaded, and pinned to the submitted query before
  // that -- so the list never changes under a keystroke that has not been asked
  // for, and never lags one behind after the first search.
  const active = docs !== null ? query.trim() : (searched ?? "");
  const result = docs === null ? null : searchCorpus(docs, active, CORPUS_LIMIT);

  const empty = NOTHING_FILED[kind];

  return (
    <PanelFrame
      label="Record"
      onClose={onClose}
      context={
        <p className="rw-panel__ctx">
          Every source this artifact stands on, filed under the question that fetched it.
        </p>
      }
    >
      <section className="rw-sect" aria-labelledby="rw-rec-filed">
        <h3 className="rw-sect__head" id="rw-rec-filed">
          <span className="rw-sect__label">Cited by this artifact</span>
          {filings.length > 0 && (
            <span className="rw-sect__count">
              {filings.length}
              <span className="rw-sr"> filings</span>
            </span>
          )}
        </h3>

        {filings.length === 0 ? (
          <PanelEmpty head={empty.head} line={empty.line} />
        ) : (
          filings.map((filing) => (
            <div className="rw-file" key={filing.key}>
              {/* The question is the group's name AND the way back to it. One
                  control rather than a heading plus a "go to turn" link, so
                  there is one thing to tab to per filing. */}
              <button
                type="button"
                className="rw-file__head"
                onClick={() => onJump(filing.key)}
                aria-label={`Go to the turn that asked: ${filing.question || "this question"}`}
              >
                <span className="rw-file__at" aria-hidden="true">
                  {filing.askedAt === null ? "—" : formatClock(filing.askedAt)}
                </span>
                <span className="rw-file__q">
                  {/* A rehydrated transcript can lose the question row, and an
                      empty heading would read as a filing nobody asked for. */}
                  {filing.question || "Question not recorded"}
                </span>
              </button>
              <ul className="rw-file__list">
                {filing.authorities.map((authority) => (
                  <AuthorityRow authority={authority} key={authority.chunkId} />
                ))}
              </ul>
            </div>
          ))
        )}
      </section>

      <section className="rw-sect" aria-labelledby="rw-rec-corpus">
        <h3 className="rw-sect__head" id="rw-rec-corpus">
          <span className="rw-sect__label">The corpus</span>
        </h3>

        <form className="rw-search" onSubmit={onSearch} role="search">
          <div className="rw-field rw-search__field">
            <SearchIcon />
            <input
              type="search"
              value={query}
              placeholder="Ingredient, dosage form, application no."
              aria-label="Search the guidance corpus"
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <button
            type="submit"
            className="rw-icon-btn rw-search__go"
            disabled={query.trim() === "" || corpus === "loading"}
            aria-label="Search the corpus"
          >
            <SendIcon />
          </button>
        </form>

        {corpus === "idle" && (
          <p className="rw-note">
            The whole FDA guidance catalog. It loads on your first search and is filtered here after
            that.
          </p>
        )}

        {corpus === "loading" && (
          <p className="rw-note" role="status">
            Loading the catalog...
          </p>
        )}

        {/* "We could not ask" is not "there is nothing". The retry says which
            of the two this is, and no count is printed beside it. */}
        {corpus === "unreachable" && (
          <p className="rw-note rw-note--fail" role="status">
            Could not reach the corpus.{" "}
            <button type="button" className="rw-note__retry" onClick={onRetry}>
              Try again
            </button>
          </p>
        )}

        {result !== null && active !== "" && (
          <>
            <p className="rw-note" role="status">
              {result.matched === 0
                ? `No guidance matches "${active}".`
                : result.matched > result.hits.length
                  ? `${result.matched} matches for "${active}". Showing the first ${result.hits.length}.`
                  : `${result.matched} ${result.matched === 1 ? "match" : "matches"} for "${active}".`}
            </p>
            {result.hits.length > 0 && (
              <ul className="rw-file__list rw-file__list--flush">
                {result.hits.map((hit) => (
                  <CorpusRow hit={hit} key={hit.id} />
                ))}
              </ul>
            )}
          </>
        )}
      </section>
    </PanelFrame>
  );
}
