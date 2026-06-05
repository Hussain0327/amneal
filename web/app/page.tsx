"use client";

import { useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { Markdown } from "@/components/Markdown";
import { askQuery, type Citation, type ClarifyOption, type QueryResponse } from "@/lib/api";

const EXAMPLES = [
  { label: "albuterol BE study", q: "What BE study design is recommended for albuterol sulfate inhalation aerosol?" },
  { label: "beclomethasone aerosol", q: "What type of study does the beclomethasone dipropionate inhalation aerosol PSG recommend?" },
  { label: "propranolol", q: "propranolol" },
  { label: "metformin dissolution", q: "What dissolution method is recommended for metformin hydrochloride?" },
];

export default function AskPage() {
  const [question, setQuestion] = useState("");
  const [ingredient, setIngredient] = useState("");
  const [dosage, setDosage] = useState("");
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(q: string, filters: Record<string, string> | null) {
    setLoading(true);
    setError(null);
    try {
      setResult(await askQuery(q, filters));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q) return;
    const filters: Record<string, string> = {};
    if (ingredient.trim()) filters["normalized_name"] = ingredient.trim().toLowerCase();
    if (dosage.trim()) filters["dosage_form"] = dosage.trim();
    void run(q, Object.keys(filters).length ? filters : null);
  }

  // A clarify option carries the exact query + filters to resend — click, no retyping.
  function onClarify(opt: ClarifyOption) {
    setQuestion(opt.query);
    setIngredient(opt.filters?.normalized_name ?? "");
    void run(opt.query, opt.filters ?? null);
  }

  return (
    <div className="measure">
      <PageHeader
        index="01"
        product="Ask"
        title="Ask the guidance corpus."
        tagline="Plain-language Q&A over FDA product-specific guidance. Every claim is cited to its source — and if a question is unclear, it asks rather than guesses."
      />

      <form onSubmit={onSubmit} className="rise d3">
        <label className="kicker" htmlFor="q" style={{ color: "var(--ink-soft)" }}>
          Inquiry
        </label>
        <textarea
          id="q"
          className="field field--inquiry"
          style={{ marginTop: "0.5rem", minHeight: "3.4rem" }}
          placeholder="propranolol   ·   What BE study design is recommended for metformin?"
          rows={2}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <div className="mt-5 flex flex-wrap items-end gap-4">
          <div className="grow" style={{ minWidth: "12rem" }}>
            <label className="kicker" style={{ color: "var(--ink-faint)" }}>
              Active ingredient · optional
            </label>
            <input
              className="field mt-1"
              value={ingredient}
              onChange={(e) => setIngredient(e.target.value)}
              placeholder="e.g. albuterol sulfate"
            />
          </div>
          <div className="grow" style={{ minWidth: "12rem" }}>
            <label className="kicker" style={{ color: "var(--ink-faint)" }}>
              Dosage form · optional
            </label>
            <input
              className="field mt-1"
              value={dosage}
              onChange={(e) => setDosage(e.target.value)}
              placeholder="e.g. inhalation aerosol"
            />
          </div>
          <button className="btn" type="submit" disabled={loading}>
            {loading ? "Consulting…" : "Submit inquiry"}
          </button>
        </div>
      </form>

      {!result && !loading && (
        <div className="rise d4 mt-7">
          <span className="kicker" style={{ color: "var(--ink-faint)" }}>
            Try
          </span>
          <div className="mt-2.5 flex flex-wrap gap-2">
            {EXAMPLES.map((ex) => (
              <button
                key={ex.q}
                className="chip"
                onClick={() => {
                  setQuestion(ex.q);
                  setIngredient("");
                  setDosage("");
                  void run(ex.q, null);
                }}
              >
                {ex.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {error && (
        <div className="stamp mt-9" style={{ borderColor: "var(--oxblood)" }}>
          <div className="stamp__tag">Request failed</div>
          <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
            {error}
          </p>
        </div>
      )}

      {result && !loading && <ResultView result={result} onClarify={onClarify} />}
    </div>
  );
}

function ResultView({
  result,
  onClarify,
}: {
  result: QueryResponse;
  onClarify: (opt: ClarifyOption) => void;
}) {
  if (result.status === "clarify") {
    return (
      <section className="mt-10 rise">
        <div className="kicker" style={{ color: "var(--gold-ink)" }}>
          Clarification requested
        </div>
        <p
          className="display"
          style={{ fontWeight: 400, fontSize: "1.3rem", lineHeight: 1.4, margin: "0.6rem 0 1.3rem" }}
        >
          {result.interpretation || result.answer}
        </p>
        <div className="flex flex-col gap-2.5">
          {result.clarify.map((opt, i) => (
            <button key={`${opt.query}::${i}`} className="opt" onClick={() => onClarify(opt)}>
              <span className="opt__no">{String(i + 1).padStart(2, "0")}</span>
              <span>{opt.label}</span>
              <span className="opt__arrow" aria-hidden>
                →
              </span>
            </button>
          ))}
        </div>
      </section>
    );
  }

  if (result.refused) {
    return (
      <section className="mt-10 rise">
        <div className="stamp doc--seal">
          <div className="stamp__tag">Declined · not in corpus</div>
          <p style={{ margin: "0.5rem 0 0", fontSize: "1.02rem", lineHeight: 1.55 }}>{result.answer}</p>
        </div>
        <p className="code mt-3" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
          audit #{result.audit_id} · {result.model_name}
        </p>
      </section>
    );
  }

  return (
    <section className="mt-10 rise">
      <div className="doc doc--seal doc--pad">
        <div className="kicker" style={{ color: "var(--gold-ink)", marginBottom: "0.6rem" }}>
          Finding
        </div>
        <Markdown>{result.answer}</Markdown>
      </div>

      <div className="mt-8">
        <div className="flex items-baseline gap-3">
          <h2 className="kicker" style={{ color: "var(--ink)" }}>
            References
          </h2>
          <hr className="hair grow" />
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            {result.citations.length} cited
          </span>
        </div>

        {result.citations.length === 0 ? (
          <p className="mt-3" style={{ color: "var(--ink-soft)", fontSize: "0.95rem" }}>
            No citations.
          </p>
        ) : (
          <div className="mt-2">
            {result.citations.map((c, i) => (
              <Reference key={`${c.short_name}-${c.page}-${i}`} n={i + 1} c={c} />
            ))}
          </div>
        )}
      </div>

      <details className="mt-7">
        <summary className="kicker" style={{ cursor: "pointer", color: "var(--ink-faint)" }}>
          Provenance
        </summary>
        <p className="code mt-2" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
          model {result.model_name} · audit #{result.audit_id} · status {result.status}
        </p>
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
          {JSON.stringify(result.citations, null, 2)}
        </pre>
      </details>
    </section>
  );
}

function Reference({ n, c }: { n: number; c: Citation }) {
  return (
    <div className="ref">
      <span className="ref__no">[{n}]</span>
      <div>
        <span className="ref__src">{c.short_name}</span>
        <span className="ref__page"> · p.{c.page}</span>
      </div>
      <blockquote className="ref__quote">{c.snippet}</blockquote>
      <a className="link code" style={{ fontSize: "0.76rem" }} href={c.source_url} target="_blank" rel="noreferrer">
        {c.source_url}
      </a>
    </div>
  );
}
