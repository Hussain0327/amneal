// Editorial masthead for each page: a mono kicker, a large serif title, a
// gold rule that draws itself in, and a tagline — revealed in sequence.
export function PageHeader({
  index,
  product,
  title,
  tagline,
}: {
  index: string;
  product: string;
  title: string;
  tagline?: string;
}) {
  return (
    <header className="mb-9">
      <div className="rise d1 flex items-baseline gap-3">
        <span className="kicker">{index}</span>
        <span className="kicker" style={{ color: "var(--ink-soft)" }}>
          REGWATCH · {product}
        </span>
      </div>
      <h1 className="display rise d2" style={{ fontSize: "clamp(2.2rem, 5vw, 3.4rem)", marginTop: "0.5rem" }}>
        {title}
      </h1>
      <hr className="rule-gold draw d2" style={{ marginTop: "0.9rem", maxWidth: "11rem" }} />
      {tagline && (
        <p
          className="rise d3"
          style={{ marginTop: "1rem", maxWidth: "44rem", color: "var(--ink-soft)", fontSize: "1.02rem", lineHeight: 1.55 }}
        >
          {tagline}
        </p>
      )}
    </header>
  );
}
