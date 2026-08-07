"use client";

import { Markdown } from "@/components/Markdown";

type DossierSection = { letter: string; title: string; body: string };
type SplitDossier = { title: string | null; intro: string; sections: DossierSection[] };

// Split the dossier markdown at its lettered "## X. Title" boundaries — the
// stable shape build_dossier emits (sections A–F). Returns null whenever the
// markdown doesn't match that shape (at least two lettered sections), so the
// caller can fall back to rendering the blob verbatim instead of mangling it.
export function splitDossier(markdown: string): SplitDossier | null {
  const parts = markdown.split(/\n(?=## )/);
  if (parts.length < 3) return null;
  const sections: DossierSection[] = [];
  for (const part of parts.slice(1)) {
    const m = part.match(/^## ([A-Z])\.\s*(.*)(?:\n|$)/);
    if (!m) return null;
    sections.push({ letter: m[1], title: m[2].trim(), body: part.slice(m[0].length) });
  }
  const head = parts[0];
  const titleMatch = head.match(/^# (.+)$/m);
  return {
    title: titleMatch ? titleMatch[1].trim() : null,
    intro: (titleMatch ? head.replace(titleMatch[0], "") : head).trim(),
    sections,
  };
}

// The compiled dossier as a bound document: a masthead carrying the scaffold
// caveat and letter-chip jumps, then each lettered section behind its own
// solid tab — the filled counterpart of the plan's dashed letters.
export function DossierView({ markdown }: { markdown: string }) {
  const split = splitDossier(markdown);
  if (!split) {
    return (
      <div className="doc doc--seal doc--pad">
        <div className="kicker" style={{ marginBottom: "0.6rem" }}>
          Dossier
        </div>
        <Markdown>{markdown}</Markdown>
      </div>
    );
  }
  // The markdown H1 is "{ingredient} dossier"; the masthead kicker already
  // says Dossier, so the title drops the redundant word (display only).
  const title = split.title ? split.title.replace(/\s+dossier$/i, "") : null;
  return (
    <article className="doc doc--seal dossier">
      <header className="dossier__mast">
        <div className="kicker">Dossier</div>
        {title && <h2 className="display dossier__title">{title}</h2>}
        <p className="dossier__scaffold">What the FDA calls for — not what your team has done</p>
        <nav className="dossier__toc" aria-label="Dossier sections">
          {split.sections.map((s) => (
            <a
              key={s.letter}
              className="dossier__jump"
              href={`#dossier-${s.letter}`}
              aria-label={`Section ${s.letter}: ${s.title}`}
            >
              {s.letter}
            </a>
          ))}
        </nav>
        {split.intro && (
          <div className="mt-3">
            <Markdown>{split.intro}</Markdown>
          </div>
        )}
      </header>
      {split.sections.map((s) => (
        <section key={s.letter} id={`dossier-${s.letter}`} className="dossier__sec">
          <span className="dossier__tab" aria-hidden>
            {s.letter}
          </span>
          <div className="dossier__body">
            <h3 className="dossier__sechead">{s.title}</h3>
            <Markdown>{s.body}</Markdown>
          </div>
        </section>
      ))}
    </article>
  );
}
