// Icon set for the Research Studio.
//
// Same grid and same stroke as the Compliance Studio's set: 16px viewBox, 1.5
// stroke, currentColor throughout, decorative by default. Only the glyphs the
// research chrome needs that the studio set does not already carry live here.
// Everything the two rooms share -- menu, caret, chat, file, book -- is imported
// from components/studio/icons instead, so one line weight governs both.

interface IconProps {
  readonly size?: number;
  readonly className?: string;
}

function svg(
  size: number,
  className: string | undefined,
  children: React.ReactNode,
): React.ReactElement {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** A dossier: more than one document, which is the whole difference between it
 * and a paper. A front sheet with a second sheet showing behind it. */
export function DossierIcon({ size = 14, className }: IconProps): React.ReactElement {
  return svg(
    size,
    className,
    <>
      <path d="M5.75 4.5h4l2.5 2.5v5.5a.75.75 0 0 1-.75.75H5.75a.75.75 0 0 1-.75-.75V5.25a.75.75 0 0 1 .75-.75z" />
      <path d="M9.75 4.5V7h2.5" />
      <path d="M3.5 10.75V3.25a.75.75 0 0 1 .75-.75h4.5" />
    </>,
  );
}

/** Revision history: a clock whose face is open at the top left, with the arrow
 * running anticlockwise out of the gap. A closed clock reads as "scheduled". */
export function HistoryIcon({ size = 15, className }: IconProps): React.ReactElement {
  return svg(
    size,
    className,
    <>
      <path d="M3.4 5.75A5.25 5.25 0 1 1 2.75 8" />
      <path d="M2.4 3.1 3.4 5.75 6.05 4.75" />
      <path d="M8 5.25V8l2.25 1.25" />
    </>,
  );
}
