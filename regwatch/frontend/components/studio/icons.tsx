// Icon set for the Compliance Studio.
//
// Line icons at a 16px grid, 1.5 stroke, currentColor throughout so every icon
// inherits the token colour of whatever control holds it. Decorative by
// default (aria-hidden): the control around them carries the accessible name.

type IconProps = { size?: number; className?: string };

function svg(size: number, className: string | undefined, children: React.ReactNode) {
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

export function CaretIcon({ size = 12, className }: IconProps) {
  return svg(size, className, <path d="M6 3.5 10.5 8 6 12.5" />);
}

export function FolderIcon({ size = 14, className }: IconProps) {
  return svg(size, className, <path d="M1.75 4.25h4l1.25 1.5h7.25v6.5a.75.75 0 0 1-.75.75H2.5a.75.75 0 0 1-.75-.75z" />);
}

export function FileIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M9.25 1.75H4.5a.75.75 0 0 0-.75.75v11a.75.75 0 0 0 .75.75h7a.75.75 0 0 0 .75-.75V4.75z" />
      <path d="M9.25 1.75v3h3" />
    </>,
  );
}

export function SearchIcon({ size = 13, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <circle cx="7" cy="7" r="4.25" />
      <path d="M10.25 10.25 13.5 13.5" />
    </>,
  );
}

export function ShieldIcon({ size = 15, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M8 1.75 13 3.5v4.25c0 3-2.1 5.35-5 6.5-2.9-1.15-5-3.5-5-6.5V3.5z" />
      <path d="M6 7.75 7.4 9.2 10.25 6" />
    </>,
  );
}

export function ChatIcon({ size = 15, className }: IconProps) {
  return svg(size, className, <path d="M13.75 8.5c0 2.35-2.35 4.25-5.25 4.25a6.7 6.7 0 0 1-1.85-.25l-3.4 1.25.9-2.7A4 4 0 0 1 2.25 8.5c0-2.35 2.35-4.25 5.25-4.25s6.25 1.9 6.25 4.25z" />);
}

export function SendIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M14 2 2 6.75l4.75 2.5L9.25 14z" />
      <path d="M14 2 6.75 9.25" />
    </>,
  );
}

export function CloseIcon({ size = 14, className }: IconProps) {
  return svg(size, className, <path d="M4 4l8 8M12 4l-8 8" />);
}

export function MenuIcon({ size = 15, className }: IconProps) {
  return svg(size, className, <path d="M2.25 4.5h11.5M2.25 8h11.5M2.25 11.5h11.5" />);
}

export function NewFolderIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M1.75 4.25h4l1.25 1.5h7.25v6.5a.75.75 0 0 1-.75.75H2.5a.75.75 0 0 1-.75-.75z" />
      <path d="M8 8v3M6.5 9.5h3" />
    </>,
  );
}

export function UploadIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M8 10.5V2.25M5 5.25 8 2.25l3 3" />
      <path d="M2.75 10.5v2.5a.75.75 0 0 0 .75.75h9a.75.75 0 0 0 .75-.75v-2.5" />
    </>,
  );
}

export function TableIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <rect x="2.25" y="3.25" width="11.5" height="9.5" rx=".75" />
      <path d="M2.25 6.5h11.5M6.5 6.5v6.25" />
    </>,
  );
}

export function FigureIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <rect x="2.25" y="3.25" width="11.5" height="9.5" rx=".75" />
      <path d="m2.75 11 3-3 2.5 2.25L10.5 8l2.75 2.5" />
      <circle cx="6" cy="6" r=".9" />
    </>,
  );
}

export function NoteIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M12.25 2.75H3.75a.75.75 0 0 0-.75.75v9a.75.75 0 0 0 .75.75h5l4.25-4.25v-5.5a.75.75 0 0 0-.75-.75z" />
      <path d="M13 9h-4v4.25" />
    </>,
  );
}

export function HighlightIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="m4.5 10.5 5.25-5.25 1.75 1.75L6.25 12.25H4.5z" />
      <path d="M2.25 14h11.5" />
    </>,
  );
}

export function ListIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M6 4.5h7.75M6 8h7.75M6 11.5h7.75" />
      <circle cx="3" cy="4.5" r=".85" fill="currentColor" stroke="none" />
      <circle cx="3" cy="8" r=".85" fill="currentColor" stroke="none" />
      <circle cx="3" cy="11.5" r=".85" fill="currentColor" stroke="none" />
    </>,
  );
}

export function NumberedIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M6.5 4.5h7.25M6.5 8h7.25M6.5 11.5h7.25" />
      <path d="M2 3.5h1v2.75M1.75 10.25h1.75l-1.75 2h1.75" strokeWidth="1.2" />
    </>,
  );
}

export function UndoIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M3 6.5h6.25a3.25 3.25 0 0 1 0 6.5H6" />
      <path d="M5.25 4 2.75 6.5l2.5 2.5" />
    </>,
  );
}

export function RedoIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M13 6.5H6.75a3.25 3.25 0 0 0 0 6.5H10" />
      <path d="M10.75 4l2.5 2.5-2.5 2.5" />
    </>,
  );
}

export function SparkIcon({ size = 14, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M8 2.25 9.3 6.2 13.25 7.5 9.3 8.8 8 12.75 6.7 8.8 2.75 7.5 6.7 6.2z" />
    </>,
  );
}

export function PinIcon({ size = 12, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M8 14V9" />
      <path d="M8 2c1.9 0 3.25 1.35 3.25 3.1S8 9 8 9 4.75 6.85 4.75 5.1 6.1 2 8 2z" />
    </>,
  );
}

export function BookIcon({ size = 12, className }: IconProps) {
  return svg(
    size,
    className,
    <>
      <path d="M3 3.25h4a1.5 1.5 0 0 1 1.5 1.5v8.25a1.25 1.25 0 0 0-1.25-1.25H3z" />
      <path d="M13 3.25H9a1.5 1.5 0 0 0-1.5 1.5v8.25A1.25 1.25 0 0 1 8.75 11.75H13z" />
    </>,
  );
}
