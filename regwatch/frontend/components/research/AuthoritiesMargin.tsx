"use client";

import { useId } from "react";

import type { Authority } from "@/lib/research-types";

// The authorities margin: the record's own marks, set BESIDE the prose.
//
// In the Compliance Studio a mark goes ON the text, because there every mark is
// the analyst's own hand -- a highlight, a tracked change, a finding anchored to
// a span. Here the marks belong to the record instead of to the reader, so they
// sit in the margin the way a source note sits on a printed page. Same paper,
// one hairline: the margin is part of the sheet and must never read as a panel
// bolted to its edge, which is why nothing here paints a background, a radius
// or a shadow of its own.
//
// The component owns no state. Which entry is lit is the sheet's fact, because
// the same fact lights a stamp out in the prose -- and two owners of one fact is
// exactly how a light ends up on in two places at once.

/**
 * DOM id of one authority entry.
 *
 * Exported because the sheet points each [n] stamp at its authority with
 * aria-describedby, and both ends have to agree on the string.
 */
export function authorityDomId(scope: string, n: number): string {
  return `${scope}-authority-${n}`;
}

interface AuthoritiesMarginProps {
  readonly authorities: readonly Authority[];
  /** The authority currently lit. Owned by the sheet, never by this component. */
  readonly litN: number | null;
  readonly onLit: (n: number | null) => void;
  /**
   * Id prefix shared with the sheet so its stamps' aria-describedby resolves.
   * Optional, so the margin still stands up on its own in a test or a fixture.
   */
  readonly scope?: string;
}

export function AuthoritiesMargin({
  authorities,
  litN,
  onLit,
  scope,
}: AuthoritiesMarginProps): React.JSX.Element | null {
  const localScope = useId();
  const idScope = scope ?? localScope;
  const headingId = `${idScope}-authorities`;

  // THE EMPTY CASE, which is a requirement and not a guard. An empty ruled
  // column would say that sources exist and are merely out of sight, which is
  // the one thing this product must never imply. So the margin does not render,
  // and the rule goes with it -- the rule is what promises the authorities. The
  // sheet says the rest in words at the foot of the prose.
  if (authorities.length === 0) {
    return null;
  }

  return (
    <aside className="rs-margin" aria-labelledby={headingId}>
      {/* Named for the reader who cannot see the hairline. Hidden rather than
          drawn: on the page the column IS the label, and a visible heading here
          would be the first piece of chrome on the paper. */}
      <h2 className="rw-sr" id={headingId}>
        Authorities cited
      </h2>
      <ul className="rs-margin__list">
        {authorities.map((authority) => (
          <li className="rs-margin__row" key={authority.n}>
            <button
              type="button"
              id={authorityDomId(idScope, authority.n)}
              className={`rs-margin__item${authority.n === litN ? " is-lit" : ""}`}
              onMouseEnter={() => onLit(authority.n)}
              onMouseLeave={() => onLit(null)}
              onFocus={() => onLit(authority.n)}
              onBlur={() => onLit(null)}
              // Pointing is the whole action, and a touch screen cannot hover.
              // The tap repeats what focus does rather than meaning something
              // else, and it is deliberately not a toggle: focus fires first, so
              // a toggle would put the light out on the way in.
              onClick={() => onLit(authority.n)}
            >
              <span className="rs-margin__n" aria-hidden="true">
                {authority.n}
              </span>
              <span className="rs-margin__body">
                {/* The visible number is the whole link to the stamp. Spoken, it
                    needs the noun with it, and 2.5.3 wants the visible text to
                    survive inside the accessible name. */}
                <span className="rw-sr">Authority {authority.n}. </span>
                <span className="rs-margin__name">{authority.shortName}</span>
                <span className="rs-margin__meta">
                  p.{authority.page} &middot;{" "}
                  {authority.recommendedDate === null
                    ? "rev not recorded"
                    : `rev ${authority.recommendedDate}`}
                </span>
                {/* The record's words, so the record's face, italic because it is
                    quoted. Clamped in CSS: an unbounded quotation in an 11rem
                    column would push the next authority off the page. */}
                <span className="rs-margin__snip">{authority.snippet}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
