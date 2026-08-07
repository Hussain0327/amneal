// The dossier's anatomy, shown before anything compiles: the REAL lettered
// sections build_dossier emits (src/regwatch/assemble/dossier.py), named
// exactly as the compiled document will name them, each with where its content
// comes from. Dashed letter tabs are the not-yet-filled state of the solid
// tabs the compiled dossier wears — the same empty-on-purpose vocabulary as
// the white paper's verified-absent cells.
const SECTIONS = [
  {
    letter: "A",
    title: "Product-Specific Guidance(s)",
    desc: "Every PSG in the corpus for this product — type, route, and recommended date, linked to source.",
    src: "corpus",
  },
  {
    letter: "B",
    title: "Extracted BE Requirements",
    desc: "Structured bioequivalence fields from each PSG, cited to the page they were read from.",
    src: "corpus",
  },
  {
    letter: "C",
    title: "Reference Listed Drug (RLD) Label",
    desc: "Brand, application number, and indications for the reference product.",
    src: "openFDA",
  },
  {
    letter: "D",
    title: "Applicable Guidance — Q&A Summary",
    desc: "A cited synthesis of what the guidance calls for on this product.",
    src: "corpus + model",
  },
  {
    letter: "E",
    title: "Dissolution Method",
    desc: "A pointer into the FDA dissolution methods database.",
    src: "FDA database",
  },
  {
    letter: "F",
    title: "Requirements Checklist (scaffold)",
    desc: "Every PSG ask as an unchecked item — what is required, never what is done.",
    src: "derived",
  },
];

export function DossierPlan({ compiling }: { compiling: boolean }) {
  return (
    <section className="plan rise d4" aria-label="Contents of a compiled dossier">
      <div className="plan__head">
        <h2 className="kicker" style={{ color: "var(--ink)" }}>
          Contents of a compiled dossier
        </h2>
        <hr className="hair grow" />
        <span className="code plan__count">six sections · A–F</span>
      </div>
      <ol className="plan__list">
        {SECTIONS.map((s) => (
          <li key={s.letter} className="plan__row">
            <span className="plan__letter code" aria-hidden>
              {s.letter}
            </span>
            <div>
              <div className="plan__title">{s.title}</div>
              <p className="plan__desc">{s.desc}</p>
            </div>
            <span className="plan__src code">{s.src}</span>
          </li>
        ))}
      </ol>
      {compiling && (
        <p className="plan__compiling" role="status">
          <span className="plan__dot" aria-hidden />
          Compiling — matching PSGs, extracting BE requirements, citing guidance…
        </p>
      )}
    </section>
  );
}
