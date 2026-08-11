"use client";

import { useCallback, useMemo, useState, type CSSProperties } from "react";

import { CaretIcon, FileIcon, FolderIcon } from "@/components/studio/icons";
import {
  countLibraryDocs,
  filterLibrary,
  type LibraryBucket,
  type LibraryDoc,
} from "@/lib/studio-library";

/** Reference-library load state, owned by the page. An error is never
 * rendered as loaded-but-empty (house convention, see the watch page). */
export type LibraryState =
  | { phase: "loading" }
  | { phase: "ready"; buckets: LibraryBucket[] }
  | { phase: "error"; message: string };

interface LibrarySectionProps {
  state: LibraryState;
  /** Trimmed, lowercased query from the shared search box; "" when idle. */
  needle: string;
  /** Active library doc id ("psg-.."), or null while a draft is on the canvas. */
  activeLibraryId: string | null;
  onOpen: (doc: LibraryDoc) => void;
  onRetry: () => void;
}

/** Same indent rule as the working tree: rows nest by padding, not containment.
 * Depth is measured from the open bucket body, which is where the drug rows
 * now hang -- the letter itself is a tile above, not a row. */
function indent(depth: number): CSSProperties {
  return { paddingLeft: `${0.4 + depth * 0.8}rem` };
}

export function LibrarySection({
  state,
  needle,
  activeLibraryId,
  onOpen,
  onRetry,
}: LibrarySectionProps) {
  // Letter buckets start CLOSED (26 buckets over ~1,800 PSGs would swamp the
  // rail), so unlike the working tree the tracked state is what the analyst
  // OPENED. Drug folders inside an opened bucket start OPEN (their PSG lists
  // are short), matching the working tree's default: track the CLOSED ones.
  // Two sets because the defaults are opposite.
  const [expandedBuckets, setExpandedBuckets] = useState<ReadonlySet<string>>(
    () => new Set<string>(),
  );
  const [collapsedDrugs, setCollapsedDrugs] = useState<ReadonlySet<string>>(
    () => new Set<string>(),
  );

  const searching = needle.length > 0;
  const visible = useMemo(() => {
    if (state.phase !== "ready") return [];
    return searching ? filterLibrary(state.buckets, needle) : state.buckets;
  }, [state, needle, searching]);

  const toggleBucket = useCallback((id: string) => {
    setExpandedBuckets((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }, []);

  const toggleDrug = useCallback((id: string) => {
    setCollapsedDrugs((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }, []);

  if (state.phase === "loading") {
    return <div className="st-tree__empty">Loading reference library...</div>;
  }
  if (state.phase === "error") {
    return (
      <div className="st-tree__empty" role="alert">
        Couldn&apos;t load the reference library. {state.message}
        <button type="button" className="st-btn st-btn--quiet st-tree__retry" onClick={onRetry}>
          Retry
        </button>
      </div>
    );
  }
  if (visible.length === 0) {
    return (
      <div className="st-tree__empty">
        {searching ? "No PSGs match that search." : "No FDA guidance documents ingested yet."}
      </div>
    );
  }

  // A search result is useless behind a closed bucket, so a live query opens
  // every surviving branch without disturbing what was toggled.
  const isBucketOpen = (bucket: LibraryBucket) => searching || expandedBuckets.has(bucket.id);

  return (
    <>
      {/* An alphabet is an index, not a list of folders: 24 full-width rows for
          a catalogue nobody browses linearly buried the 7 documents under
          review. The bodies follow the whole grid rather than each tile, which
          keeps the index intact while a letter is open. */}
      <div className="st-lib__grid">
        {visible.map((bucket) => {
          const bucketOpen = isBucketOpen(bucket);
          const bucketDocs = countLibraryDocs([bucket]);
          return (
            <button
              key={bucket.id}
              type="button"
              className={`st-lib__tile${bucketOpen ? " is-open" : ""}`}
              aria-expanded={bucketOpen}
              aria-controls={`st-lib-${bucket.id}`}
              // Adjacent spans concatenate to "A2" in the computed name; say
              // what the tile means instead.
              aria-label={`${bucket.letter} - ${bucketDocs} ${bucketDocs === 1 ? "PSG" : "PSGs"}`}
              onClick={() => toggleBucket(bucket.id)}
            >
              <span className="st-lib__letter">{bucket.letter}</span>
              <span className="st-lib__count">{bucketDocs}</span>
            </button>
          );
        })}
      </div>

      <div className="st-lib__bodies">
        {visible.map((bucket) => (
          <div key={bucket.id} id={`st-lib-${bucket.id}`} hidden={!isBucketOpen(bucket)}>
            {bucket.drugs.map((drug) => {
              const drugOpen = searching || !collapsedDrugs.has(drug.id);
              const drugBodyId = `st-lib-${drug.id}`;
              return (
                <div key={drug.id}>
                  <button
                    type="button"
                    className="st-node st-node--folder"
                    style={indent(0)}
                    aria-expanded={drugOpen}
                    aria-controls={drugBodyId}
                    onClick={() => toggleDrug(drug.id)}
                  >
                    <CaretIcon
                      className={`st-node__caret${drugOpen ? " st-node__caret--open" : ""}`}
                    />
                    <FolderIcon className="st-node__icon" />
                    <span className="st-node__label">{drug.label}</span>
                  </button>
                  <div id={drugBodyId} hidden={!drugOpen}>
                    {drug.docs.map((doc) => {
                      const isActive = doc.id === activeLibraryId;
                      return (
                        <button
                          key={doc.id}
                          type="button"
                          className={`st-node st-node--doc${isActive ? " is-active" : ""}`}
                          style={indent(1)}
                          aria-current={isActive ? "true" : undefined}
                          onClick={() => onOpen(doc)}
                        >
                          <FileIcon className="st-node__icon" />
                          <span className="st-node__label">{doc.label}</span>
                          {doc.recommendedDate ? (
                            <span className="st-node__date">{doc.recommendedDate}</span>
                          ) : null}
                          <span className={`st-chip st-node__badge st-node__badge--${doc.psgType}`}>
                            {doc.psgType === "final" ? "Final" : "Draft"}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </>
  );
}
