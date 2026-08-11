"use client";

import { useMemo, useState } from "react";

import { DossierIcon } from "@/components/research/icons";
import { CaretIcon, ChatIcon, FileIcon } from "@/components/studio/icons";
import type { ArtifactKind, KindGroup, WorkItem } from "@/lib/research-types";

/** Singular nouns, for the one place a count can be 1. The plurals come off
 * KindGroup.label instead of a second table, so the two can never disagree. */
const KIND_NOUN: Record<ArtifactKind, string> = {
  thread: "thread",
  dossier: "dossier",
  bulletin: "bulletin",
  paper: "paper",
};

/** An empty group is an invitation, not a mood. Bulletins get the one line that
 * is not an invitation, because there is nothing to invite: they arrive. */
const EMPTY_COPY: Record<ArtifactKind, string> = {
  thread: "Ask a question to start a thread.",
  dossier: "Build a dossier to gather sources on one product.",
  bulletin: "No bulletins yet. They arrive when FDA publishes.",
  paper: "Draft a white paper from a dossier.",
};

interface MakeAction {
  readonly kind: ArtifactKind;
  readonly label: string;
  readonly Icon: React.ComponentType<{ readonly className?: string }>;
}

/** The verbs the rail no longer carries in its group names. Bulletins are
 * absent on purpose: you do not author one. */
const MAKE_ACTIONS: readonly MakeAction[] = [
  { kind: "thread", label: "Ask", Icon: ChatIcon },
  { kind: "dossier", label: "Dossier", Icon: DossierIcon },
  { kind: "paper", label: "White paper", Icon: FileIcon },
];

/**
 * The group header's accessible name. Every visible string in the header has to
 * appear here, because aria-label replaces the contents outright -- and the
 * three states have to be three different sentences. "Unavailable" spelled out
 * is the whole point of the component: a screen reader that hears "Dossiers, 0"
 * when the request failed has been told something false.
 *
 * Pure and exported so the three states can be asserted without a DOM.
 */
export function kindHeadLabel(group: KindGroup): string {
  if (group.state === "loading") return `${group.label}, loading`;
  if (group.state === "unreachable") {
    return `${group.label}, unavailable. We could not reach the server, so this is not a count of zero.`;
  }
  const count = group.items.length;
  return `${count} ${count === 1 ? KIND_NOUN[group.kind] : group.label.toLowerCase()}`;
}

interface WorkRailProps {
  readonly groups: readonly KindGroup[];
  /** Id of the artifact on the sheet, or null before anything is opened. */
  readonly activeId: string | null;
  onSelect: (item: WorkItem) => void;
  onMake: (kind: ArtifactKind) => void;
  /**
   * Reload one kind after `state: "unreachable"`. Optional so the four-prop
   * contract still type-checks, but a rail wired without it shows the failure
   * and offers no way out of it -- pass it.
   */
  onRetry?: (kind: ArtifactKind) => void;
}

export function WorkRail({
  groups,
  activeId,
  onSelect,
  onMake,
  onRetry,
}: WorkRailProps): React.ReactElement {
  const activeKind = useMemo(
    () => groups.find((group) => group.items.some((item) => item.id === activeId))?.kind ?? null,
    [groups, activeId],
  );

  // One group open at a time: the rail's job is to show what you are working
  // on, and four open accordions is a list of everything again.
  const [openKind, setOpenKind] = useState<ArtifactKind | null>(activeKind);
  const [seenActiveId, setSeenActiveId] = useState<string | null>(activeId);

  // Adjusted during render rather than in an effect. When the page opens an
  // artifact from somewhere other than this rail -- onMake, a deep link, a
  // bulletin notification -- the rail has to be showing that artifact's group
  // on the first paint. An effect would paint the wrong group first and correct
  // it, which reads as a flicker in the one element that says where you are.
  if (activeId !== seenActiveId) {
    setSeenActiveId(activeId);
    if (activeKind !== null) setOpenKind(activeKind);
  }

  return (
    <aside className="rw-work" aria-label="Your work">
      <div className="rw-work__head">
        <h2 className="rw-eyebrow">Work</h2>
      </div>

      <div className="rw-work__scroll">
        {groups.map((group) => {
          const isOpen = openKind === group.kind;
          const bodyId = `rw-work-${group.kind}`;
          return (
            <div className="rw-work__group" key={group.kind}>
              <button
                type="button"
                className={`rw-work__kind${isOpen ? " is-open" : ""}`}
                aria-expanded={isOpen}
                aria-controls={bodyId}
                aria-label={kindHeadLabel(group)}
                onClick={() => setOpenKind(isOpen ? null : group.kind)}
              >
                <CaretIcon
                  className={`rw-work__caret${isOpen ? " rw-work__caret--open" : ""}`}
                />
                <span className="rw-work__label" aria-hidden="true">
                  {group.label}
                </span>
                {/* Zero and "we could not ask" are different facts, and an
                    analyst who reads one as the other stops looking. So the
                    count renders only when there really is a count: loading
                    shows the pulsing dot alone, unreachable shows a word. */}
                {group.state === "ready" ? (
                  <span className="rw-work__count" aria-hidden="true">
                    {group.items.length}
                  </span>
                ) : null}
                {group.state === "unreachable" ? (
                  <span className="rw-work__flag" aria-hidden="true">
                    Unavailable
                  </span>
                ) : null}
                {/* Last, and therefore flush right on every row whatever sits
                    to its left. Put before the count and the dot would jump a
                    column on the one row that has no count, which is the row
                    whose state matters most. */}
                <span
                  className={`rw-glyph rw-work__dot rw-work__dot--${group.state}`}
                  aria-hidden="true"
                />
              </button>

              {group.state === "unreachable" ? (
                <p className="rw-work__lost" role="alert">
                  We could not load your {group.label.toLowerCase()}.
                  {onRetry ? (
                    <button
                      type="button"
                      className="rw-btn rw-btn--quiet rw-work__retry"
                      aria-label={`Retry loading ${group.label.toLowerCase()}`}
                      onClick={() => onRetry(group.kind)}
                    >
                      Retry
                    </button>
                  ) : null}
                </p>
              ) : null}

              <div id={bodyId} className="rw-work__items" hidden={!isOpen}>
                {group.items.map((item) => {
                  const isActive = item.id === activeId;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className={`rw-work__item${isActive ? " is-active" : ""}`}
                      aria-current={isActive ? "true" : undefined}
                      onClick={() => onSelect(item)}
                    >
                      <span className="rw-work__item-title">{item.title}</span>
                      <span className="rw-work__when">{item.updatedAt}</span>
                    </button>
                  );
                })}
                {group.state === "ready" && group.items.length === 0 ? (
                  <p className="rw-work__empty">{EMPTY_COPY[group.kind]}</p>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>

      <div className="rw-work__make">
        <h2 className="rw-eyebrow">Make</h2>
        {MAKE_ACTIONS.map(({ kind, label, Icon }) => (
          <button
            key={kind}
            type="button"
            className="rw-work__make-btn"
            onClick={() => onMake(kind)}
          >
            <Icon className="rw-work__make-icon" />
            <span>{label}</span>
          </button>
        ))}
        <p className="rw-work__make-note">Bulletins arrive on their own.</p>
      </div>
    </aside>
  );
}
