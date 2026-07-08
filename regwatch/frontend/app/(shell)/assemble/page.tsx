"use client";

import { useEffect, useRef, useState } from "react";

import { useCurrentProduct } from "@/components/CurrentProductProvider";
import { PageHeader } from "@/components/PageHeader";
import { Markdown } from "@/components/Markdown";
import { assemble, type AssembleResponse } from "@/lib/api";

export default function AssemblePage() {
  // The RLD field maps to the scoped reference product: prefill it (application
  // number preferred, else the product name) so a product scoped elsewhere
  // carries in, and keep it in sync as the scope changes in place — unless the
  // analyst has edited the field (the dirty-guard below). This surface only
  // READS the scope, never writes it back, because an "ingredient + brand/appl.
  // no." intake can't honestly produce both halves of the scope.
  const { applicationNumber, referenceProductName } = useCurrentProduct();
  const scopeRld = applicationNumber || referenceProductName;
  const [ingredient, setIngredient] = useState("");
  const [dosage, setDosage] = useState("");
  const [rld, setRld] = useState(() => scopeRld);
  const lastScopeRld = useRef(scopeRld);
  useEffect(() => {
    // Adopt a NEW non-empty scope onto an untouched field; never let a scope
    // *clear* blank a prefilled-but-untouched field. Advance the ref ONLY when a
    // real scope arrives: bailing on an empty scope keeps the guard pointed at
    // the field's current value, so a clear followed by a distinct scope is still
    // recognized as "untouched" and adopted (rather than desyncing the ref and
    // freezing the field forever).
    if (!scopeRld) return;
    setRld((cur) => (cur === lastScopeRld.current ? scopeRld : cur));
    lastScopeRld.current = scopeRld;
  }, [scopeRld]);
  const [result, setResult] = useState<AssembleResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!ingredient.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setResult(await assemble(ingredient.trim(), dosage.trim() || null, rld.trim() || null));
    } catch (er) {
      setError(er instanceof Error ? er.message : String(er));
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="measure">
      <PageHeader
        index="02"
        product="Assemble"
        title="Compile a cited dossier."
        tagline="A scaffold of what the FDA calls for on a target product — assembled and cited from the guidance corpus. It states what's required, not what your team has done."
      />

      <form onSubmit={onSubmit} className="doc doc--pad rise d3">
        <div className="kicker" style={{ color: "var(--gold-ink)" }}>
          Intake
        </div>
        <div className="mt-4 grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(13rem, 1fr))" }}>
          <Field id="ingredient" label="Active ingredient" value={ingredient} onChange={setIngredient} placeholder="albuterol sulfate" />
          <Field id="dosage" label="Dosage form · optional" value={dosage} onChange={setDosage} placeholder="inhalation aerosol" />
          <Field id="rld" label="RLD · brand or appl. no. · optional" value={rld} onChange={setRld} placeholder="e.g. 020503" />
        </div>
        <div className="mt-5">
          <button className="btn" type="submit" disabled={loading || !ingredient.trim()}>
            {loading ? "Compiling…" : "Compile dossier"}
          </button>
        </div>
      </form>

      {error && (
        <div className="stamp mt-8" role="alert">
          <div className="stamp__tag">Request failed</div>
          <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
            {error}
          </p>
        </div>
      )}

      {result && !loading && (
        <section className="mt-9 rise">
          {result.refused ? (
            <div className="stamp doc--seal">
              <div className="stamp__tag">Insufficient basis</div>
              <div className="mt-2">
                <Markdown>{result.markdown}</Markdown>
              </div>
            </div>
          ) : (
            <div className="doc doc--seal doc--pad">
              <div className="kicker" style={{ color: "var(--gold-ink)", marginBottom: "0.6rem" }}>
                Dossier
              </div>
              <Markdown>{result.markdown}</Markdown>
            </div>
          )}
          <details className="mt-6">
            <summary className="kicker" style={{ cursor: "pointer", color: "var(--ink-faint)" }}>
              Raw sections
            </summary>
            <pre
              className="code mt-2"
              style={{
                fontSize: "0.7rem",
                background: "var(--paper-3)",
                border: "1px solid var(--edge)",
                borderRadius: "2px",
                padding: "0.8rem",
                overflow: "auto",
                color: "var(--ink-2)",
              }}
            >
              {JSON.stringify(result.sections, null, 2)}
            </pre>
          </details>
        </section>
      )}
    </div>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  placeholder,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div>
      <label className="kicker" htmlFor={id} style={{ color: "var(--ink-faint)" }}>
        {label}
      </label>
      <input id={id} className="field mt-1" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
    </div>
  );
}
