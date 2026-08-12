"use client";

import { useCallback, useEffect, useId, useRef, type ReactNode, type SyntheticEvent } from "react";

import { AuthoritiesMargin, authorityDomId } from "@/components/research/AuthoritiesMargin";
import type { Authority } from "@/lib/research-types";

// The sheet: a desk, a page on it, and the page ruled into prose and margin.
//
// This is the one thing the Research Studio has that the Compliance Studio does
// not, so it is the only place in either building where the design spends. The
// rest of the surface is quiet on purpose.
//
// WHAT MAKES THE PAGE AN OBJECT
// Paper on parchment is 1.21:1, so fill cannot carry the separation and the
// three mechanisms in tokens.css have to do it instead: the contact shadow, the
// --rw-paper-edge hairline drawn as that shadow's first layer, and the blotter
// edge on the desk, which darkens at the OUTER edges only so the ground never
// rises to meet the page. Two of the three are in --rw-shadow-page; the third
// is .rs-desk::before. Remove any one and the sheet goes flat.
//
// HOW A STAMP FINDS ITS AUTHORITY
// The prose is the caller's -- streamed markdown on a thread, a fixed section on
// a dossier -- so the sheet cannot hand a stamp a prop. It reads one attribute
// instead: ANY element in the prose carrying data-authority-n is a stamp for
// that authority, whoever rendered it. From that single fact the sheet gets all
// three halves of the link: it delegates hover and focus off the prose column,
// it lights the matching stamp with one generated rule, and it writes the
// aria-describedby that makes the relationship survive without a pointer.

/** Any element in the prose that claims to be the [n] stamp for an authority. */
const STAMP_SELECTOR = "[data-authority-n]";

/** The [n] a stamp claims, or null when the event did not start on one. */
function stampNumber(target: EventTarget | null): number | null {
  if (!(target instanceof Element)) {
    return null;
  }
  const stamp = target.closest(STAMP_SELECTOR);
  if (stamp === null) {
    return null;
  }
  const n = Number(stamp.getAttribute("data-authority-n"));
  return Number.isInteger(n) && n > 0 ? n : null;
}

/**
 * The one rule that lights a stamp, scoped to this sheet by its generated id.
 *
 * A rule rather than a class because the sheet does not own the elements it has
 * to light, and reaching into the caller's DOM to toggle a class loses the race
 * with the next render of a streaming answer. `n` reaches the stylesheet only
 * after being matched, as an integer, against this turn's own authorities.
 */
function litStampRule(scope: string, n: number): string {
  // Fill and ink only. The border is the stamp's ONLY boundary against the
  // paper and css/sheet.css sets it to --rw-gold for the 3:1 that 1.4.11 owes
  // a control edge; --rw-gold-deep is 2.90:1 there, so lighting a stamp must
  // not quietly hand that back. The lit state is carried by the fill going from
  // wash to gold, which is a far larger change than an edge.
  return (
    `[data-rs-sheet="${scope}"] [data-authority-n="${n}"] {` +
    "background: var(--rw-gold-fill);" +
    "color: var(--rw-ink);" +
    "}"
  );
}

interface SheetProps {
  /** The artefact kind, set in mono above the title. */
  readonly kicker: string;
  readonly title: string;
  readonly authorities: readonly Authority[];
  /**
   * Whether the sheet is showing a turn that has finished arriving.
   *
   * "Not sourced" is a VERDICT about a turn, so it can only be stated once
   * there is one and it has stopped moving. An empty margin over a streaming
   * answer, an opening thread, or a sheet nobody has asked anything means "not
   * yet", and saying "nothing here is drawn from the record" about prose that
   * has not arrived is the same class of lie the margin's own empty case exists
   * to prevent. Required rather than defaulted: the sheet cannot know, and a
   * caller that has not thought about it should not compile.
   */
  readonly settled: boolean;
  /** The authority the reader is pointing at, in either column. The sheet is
      the single owner of this fact; neither column keeps its own copy. */
  readonly litN: number | null;
  readonly onLit: (n: number | null) => void;
  /** The composer. Docked below the sheet, on the desk, never on the paper. */
  readonly footer: ReactNode;
  /** The prose column. */
  readonly children: ReactNode;
}

export function Sheet({
  kicker,
  title,
  authorities,
  settled,
  litN,
  onLit,
  footer,
  children,
}: SheetProps): React.JSX.Element {
  const scope = useId();
  const proseRef = useRef<HTMLDivElement | null>(null);
  const sourced = authorities.length > 0;

  // Matched, not trusted: only an integer that is actually one of this turn's
  // authorities is ever interpolated into a stylesheet.
  const litAuthority =
    authorities.find((authority) => authority.n === litN && Number.isInteger(authority.n)) ?? null;

  const handleStampOn = useCallback(
    (event: SyntheticEvent<HTMLDivElement>): void => {
      const n = stampNumber(event.target);
      if (n !== null) {
        onLit(n);
      }
    },
    [onLit],
  );

  const handleStampOff = useCallback(
    (event: SyntheticEvent<HTMLDivElement>): void => {
      if (stampNumber(event.target) !== null) {
        onLit(null);
      }
    },
    [onLit],
  );

  // Hover is not a fact a screen reader can reach, so the link is written into
  // the markup as well: each stamp is described by the authority it points at.
  // The stamps belong to the caller, so the sheet writes the attribute after
  // render and leaves alone any the caller set itself.
  //
  // Observed rather than keyed on `children`: a streamed answer arrives inside a
  // child that re-renders on its own, so the prose can gain a stamp at token 300
  // without this component rendering at all. The observer is disconnected on
  // every exit, including the unsourced early return, which cannot leave one
  // running because it never starts one.
  useEffect(() => {
    const prose = proseRef.current;
    if (prose === null || !sourced) {
      return;
    }
    const wire = (): void => {
      for (const stamp of prose.querySelectorAll(STAMP_SELECTOR)) {
        const n = stampNumber(stamp);
        if (n === null || stamp.hasAttribute("aria-describedby")) {
          continue;
        }
        stamp.setAttribute("aria-describedby", authorityDomId(scope, n));
      }
    };
    wire();
    const observer = new MutationObserver(wire);
    observer.observe(prose, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [scope, sourced]);

  return (
    <div className="rs-desk">
      <div className="rs-desk__scroll">
        <article className="rs-sheet" data-rs-sheet={scope}>
          {litAuthority !== null && <style>{litStampRule(scope, litAuthority.n)}</style>}
          <div className={`rs-sheet__grid${sourced ? "" : " rs-sheet__grid--unruled"}`}>
            <header className="rs-sheet__head">
              <p className="rs-sheet__kicker">{kicker}</p>
              <h1 className="rs-sheet__title">{title}</h1>
            </header>

            {/* Delegated rather than bound per stamp: the sheet does not know how
                many stamps there are, or when the next one arrives. onMouseOver /
                onMouseOut are the bubbling pair -- enter and leave do not
                delegate -- and focus and blur carry the same two calls so the
                link is identical from the keyboard. */}
            <div
              className="rs-sheet__prose"
              ref={proseRef}
              onMouseOver={handleStampOn}
              onMouseOut={handleStampOff}
              onFocus={handleStampOn}
              onBlur={handleStampOff}
            >
              {children}

              {/* THE UNSOURCED TURN. The margin is gone and so is the rule; if
                  the sheet stopped there, the absence would read as a layout
                  that happens to have nothing in it. It says the thing instead,
                  in the app's own face because this is the app speaking and not
                  the record, and it names what to do next.

                  Gated on `settled` as well as on the array: an empty margin
                  before the turn lands means "not yet", and this sentence is a
                  verdict. Unsourced and unfinished look identical from here,
                  which is exactly why the sheet is told rather than guessing. */}
              {!sourced && settled && (
                <div className="rs-sheet__unsourced">
                  <span className="rs-sheet__unsourced-label">Not sourced</span>
                  <p className="rs-sheet__unsourced-line">
                    Nothing on this sheet is drawn from the record. Narrow the question, or open the
                    record to see what is there.
                  </p>
                </div>
              )}
            </div>

            {sourced && (
              <AuthoritiesMargin
                authorities={authorities}
                litN={litN}
                onLit={onLit}
                scope={scope}
              />
            )}
          </div>
        </article>
      </div>

      {footer !== null && footer !== undefined && (
        <div className="rs-desk__dock">
          <div className="rs-desk__dock-inner">{footer}</div>
        </div>
      )}
    </div>
  );
}
