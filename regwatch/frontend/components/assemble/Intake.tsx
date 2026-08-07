"use client";

// The compilation order (surface 02 intake): the active ingredient is the
// headline, set in the same inquiry serif Ask uses; the qualifiers sit
// subordinate; the seal action closes the form bottom-right. Fully controlled
// by the caller — this component owns only the markup, so the design fixtures
// can render it without the page's scope wiring.
export function Intake({
  ingredient,
  dosage,
  rld,
  onIngredient,
  onDosage,
  onRld,
  onSubmit,
  loading,
}: {
  ingredient: string;
  dosage: string;
  rld: string;
  onIngredient: (v: string) => void;
  onDosage: (v: string) => void;
  onRld: (v: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  loading: boolean;
}) {
  return (
    <form onSubmit={onSubmit} className="doc doc--pad rise d3">
      <div className="kicker">Intake</div>
      <label
        className="kicker mt-4"
        htmlFor="ingredient"
        style={{ display: "block", color: "var(--ink-faint)" }}
      >
        Active ingredient
      </label>
      <input
        id="ingredient"
        className="field field--inquiry order__lead mt-1"
        value={ingredient}
        onChange={(e) => onIngredient(e.target.value)}
        placeholder="albuterol sulfate"
      />
      <div className="order__quals">
        <Field
          id="dosage"
          label="Dosage form"
          optional
          value={dosage}
          onChange={onDosage}
          placeholder="inhalation aerosol"
        />
        <Field
          id="rld"
          label="Reference listed drug"
          optional
          value={rld}
          onChange={onRld}
          placeholder="brand or application no. — e.g. 020503"
        />
      </div>
      <div className="order__foot">
        <p className="order__creed">Compiled from the guidance corpus — nothing is invented.</p>
        <button className="btn" type="submit" disabled={loading || !ingredient.trim()}>
          {loading ? "Compiling…" : "Compile dossier"}
        </button>
      </div>
    </form>
  );
}

function Field({
  id,
  label,
  optional,
  value,
  onChange,
  placeholder,
}: {
  id: string;
  label: string;
  optional?: boolean;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div>
      <label className="kicker" htmlFor={id} style={{ color: "var(--ink-faint)" }}>
        {label}
        {optional && <span className="order__opt">optional</span>}
      </label>
      <input
        id={id}
        className="field mt-1"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
    </div>
  );
}
