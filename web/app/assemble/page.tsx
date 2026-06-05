"use client";

import { useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { Markdown } from "@/components/Markdown";
import { assemble, type AssembleResponse } from "@/lib/api";

export default function AssemblePage() {
  const [ingredient, setIngredient] = useState("");
  const [dosage, setDosage] = useState("");
  const [rld, setRld] = useState("");
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
    <div className="max-w-3xl">
      <PageHeader
        product="Assemble"
        tagline="Build a cited dossier for a target product — a scaffold of what the FDA calls for, not what your team has done."
      />

      <form onSubmit={onSubmit} className="space-y-3">
        <div className="grid grid-cols-3 gap-3">
          <input
            className="amneal-input"
            placeholder="Active ingredient"
            value={ingredient}
            onChange={(e) => setIngredient(e.target.value)}
          />
          <input
            className="amneal-input"
            placeholder="Dosage form (optional)"
            value={dosage}
            onChange={(e) => setDosage(e.target.value)}
          />
          <input
            className="amneal-input"
            placeholder="RLD brand or appl. no. (optional)"
            value={rld}
            onChange={(e) => setRld(e.target.value)}
          />
        </div>
        <button className="amneal-btn" type="submit" disabled={loading}>
          {loading ? "Assembling…" : "Build dossier"}
        </button>
      </form>

      {error && (
        <div className="amneal-card mt-6 text-sm text-red-700" style={{ borderLeftColor: "#dc2626" }}>
          {error}
        </div>
      )}

      {result && !loading && (
        <section className="mt-6">
          {result.refused ? (
            <div
              className="amneal-card text-amber-800"
              style={{ borderLeftColor: "#d97706", background: "#fffbeb" }}
            >
              <Markdown>{result.markdown}</Markdown>
            </div>
          ) : (
            <div className="amneal-card">
              <Markdown>{result.markdown}</Markdown>
            </div>
          )}
          <details className="mt-5">
            <summary className="cursor-pointer text-sm text-ink-soft">Raw sections</summary>
            <pre className="mt-2 overflow-auto rounded bg-gold-soft p-3 text-xs">
              {JSON.stringify(result.sections, null, 2)}
            </pre>
          </details>
        </section>
      )}
    </div>
  );
}
