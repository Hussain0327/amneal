"use client";

import { useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { Markdown } from "@/components/Markdown";
import { askQuery, type Citation, type ClarifyOption, type QueryResponse } from "@/lib/api";

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

  // A clarify option carries the exact query + filters to resend — the user
  // clicks instead of retyping (the load-bearing UX from the Streamlit port).
  function onClarify(opt: ClarifyOption) {
    setQuestion(opt.query);
    setIngredient(opt.filters?.normalized_name ?? "");
    void run(opt.query, opt.filters ?? null);
  }

  return (
    <div className="max-w-3xl">
      <PageHeader
        product="Ask"
        tagline="Plain-language Q&A over the FDA guidance corpus. Every claim is cited — it'll guide you if it needs more."
      />

      <form onSubmit={onSubmit} className="space-y-3">
        <textarea
          className="amneal-input min-h-[90px]"
          placeholder="propranolol   ·   What BE study design is recommended for metformin?"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <div className="grid grid-cols-2 gap-3">
          <input
            className="amneal-input"
            placeholder="Filter: active ingredient (optional)"
            value={ingredient}
            onChange={(e) => setIngredient(e.target.value)}
          />
          <input
            className="amneal-input"
            placeholder="Filter: dosage form (optional)"
            value={dosage}
            onChange={(e) => setDosage(e.target.value)}
          />
        </div>
        <button className="amneal-btn" type="submit" disabled={loading}>
          {loading ? "Thinking…" : "Ask"}
        </button>
      </form>

      {error && (
        <div className="amneal-card mt-6 text-sm text-red-700" style={{ borderLeftColor: "#dc2626" }}>
          {error}
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
      <section className="mt-6 space-y-3">
        <div className="amneal-card">{result.interpretation || result.answer}</div>
        <div className="flex flex-col gap-2">
          {result.clarify.map((opt, i) => (
            <button
              key={`${opt.query}::${i}`}
              className="amneal-btn text-left"
              onClick={() => onClarify(opt)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </section>
    );
  }

  return (
    <section className="mt-6">
      {result.refused ? (
        <div
          className="amneal-card text-amber-800"
          style={{ borderLeftColor: "#d97706", background: "#fffbeb" }}
        >
          {result.answer}
        </div>
      ) : (
        <div className="amneal-card">
          <Markdown>{result.answer}</Markdown>
        </div>
      )}

      <h2 className="mb-2 mt-6 text-xl font-bold text-ink">Sources</h2>
      {result.citations.length === 0 ? (
        <p className="text-sm text-ink-soft">No citations.</p>
      ) : (
        <ul className="space-y-2">
          {result.citations.map((c, i) => (
            <CitationItem key={`${c.short_name}-${c.page}-${i}`} c={c} />
          ))}
        </ul>
      )}

      <details className="mt-5">
        <summary className="cursor-pointer text-sm text-ink-soft">Response detail</summary>
        <p className="mt-2 text-xs text-ink-soft">
          model: {result.model_name} · audit_id: {result.audit_id} · status: {result.status}
        </p>
        <pre className="mt-2 overflow-auto rounded bg-gold-soft p-3 text-xs">
          {JSON.stringify(result.citations, null, 2)}
        </pre>
      </details>
    </section>
  );
}

function CitationItem({ c }: { c: Citation }) {
  return (
    <li className="amneal-card">
      <div className="font-semibold">
        {c.short_name}, p.{c.page} —{" "}
        <a
          className="font-medium text-gold-deep underline"
          href={c.source_url}
          target="_blank"
          rel="noreferrer"
        >
          {c.source_url}
        </a>
      </div>
      <blockquote className="mt-1 border-l-2 border-gold-soft pl-3 text-sm text-ink-soft">
        {c.snippet}
      </blockquote>
    </li>
  );
}
