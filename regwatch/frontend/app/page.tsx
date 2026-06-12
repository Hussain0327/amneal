"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { AnswerFeedback } from "@/components/AnswerFeedback";
import { PageHeader } from "@/components/PageHeader";
import { Markdown } from "@/components/Markdown";
import { useSessions } from "@/components/SessionsProvider";
import {
  askQuery,
  getSession,
  type ChatMessage,
  type Citation,
  type ClarifyOption,
  type QueryResponse,
  type QueryStatus,
} from "@/lib/api";

const EXAMPLES = [
  { label: "albuterol BE study", q: "What BE study design is recommended for albuterol sulfate inhalation aerosol?" },
  { label: "beclomethasone aerosol", q: "What type of study does the beclomethasone dipropionate inhalation aerosol PSG recommend?" },
  { label: "propranolol", q: "propranolol" },
  { label: "metformin dissolution", q: "What dissolution method is recommended for metformin hydrochloride?" },
];

// One rendered turn of the conversation. Live assistant turns carry clarify
// options + provenance; turns rehydrated from GET /sessions/{id} carry only
// content / status / citations (same Citation shape as a live answer).
interface Turn {
  role: "user" | "assistant";
  content: string;
  status: QueryStatus | null;
  refused: boolean;
  citations: Citation[];
  clarify: ClarifyOption[];
  interpretation: string | null;
  meta: { model_name: string; audit_id: number; turn_id: string } | null;
}

const STATUSES: readonly string[] = ["answer", "summary", "clarify", "scope_warning", "refused"];

function turnFromMessage(m: ChatMessage): Turn {
  const status = m.status && STATUSES.includes(m.status) ? (m.status as QueryStatus) : null;
  return {
    role: m.role,
    content: m.content,
    status,
    refused: status === "refused",
    citations: m.citations ?? [],
    clarify: [],
    interpretation: null,
    meta: null,
  };
}

function userTurn(q: string): Turn {
  return { role: "user", content: q, status: null, refused: false, citations: [], clarify: [], interpretation: null, meta: null };
}

function assistantTurn(r: QueryResponse): Turn {
  return {
    role: "assistant",
    content: r.answer,
    status: r.status,
    refused: r.refused || r.status === "refused",
    citations: r.citations,
    clarify: r.clarify,
    interpretation: r.interpretation,
    meta: { model_name: r.model_name, audit_id: r.audit_id, turn_id: r.turn_id },
  };
}

export default function AskPage() {
  // useSearchParams needs a Suspense boundary during prerender.
  return (
    <Suspense fallback={null}>
      <AskView />
    </Suspense>
  );
}

function AskView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlSession = searchParams.get("session");
  const { refresh: refreshSessions, setActiveSessionId } = useSessions();

  const [question, setQuestion] = useState("");
  const [ingredient, setIngredient] = useState("");
  const [dosage, setDosage] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Mirrors sessionId so the URL-sync effect can tell "we just created this
  // session live" (skip refetch) from "another session was selected" (fetch).
  const sessionIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!urlSession) {
      // New chat: back to the empty state.
      sessionIdRef.current = null;
      setSessionId(null);
      setTurns([]);
      setError(null);
      setActiveSessionId(null);
      return;
    }
    if (urlSession === sessionIdRef.current) {
      setActiveSessionId(urlSession);
      return;
    }
    let cancelled = false;
    setHistoryLoading(true);
    setError(null);
    getSession(urlSession)
      .then((d) => {
        if (cancelled) return;
        sessionIdRef.current = urlSession;
        setSessionId(urlSession);
        setTurns(d.messages.map(turnFromMessage));
        setActiveSessionId(urlSession);
        void refreshSessions();
      })
      .catch((e) => {
        if (cancelled) return;
        setTurns([]);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [urlSession, refreshSessions, setActiveSessionId]);

  async function run(q: string, filters: Record<string, string> | null) {
    setLoading(true);
    setError(null);
    try {
      const next = await askQuery(q, filters, sessionIdRef.current);
      sessionIdRef.current = next.session_id;
      setSessionId(next.session_id);
      setTurns((prev) => [...prev, userTurn(q), assistantTurn(next)]);
      setQuestion("");
      setActiveSessionId(next.session_id);
      if (urlSession !== next.session_id) {
        router.replace(`/?session=${encodeURIComponent(next.session_id)}`, { scroll: false });
      }
      void refreshSessions();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || loading) return;
    const filters: Record<string, string> = {};
    if (ingredient.trim()) filters["normalized_name"] = ingredient.trim().toLowerCase();
    if (dosage.trim()) filters["dosage_form"] = dosage.trim();
    void run(q, Object.keys(filters).length ? filters : null);
  }

  // A clarify option carries the exact query + filters to resend — click, no retyping.
  function onClarify(opt: ClarifyOption) {
    setIngredient(opt.filters?.normalized_name ?? "");
    void run(opt.query, opt.filters ?? null);
  }

  const empty = turns.length === 0 && !loading && !historyLoading;

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

      {empty && (
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

      {historyLoading && (
        <p className="code mt-8" style={{ fontSize: "0.74rem", color: "var(--ink-faint)" }}>
          Opening conversation…
        </p>
      )}

      {turns.length > 0 && (
        <section className="mt-2">
          {turns.map((t, i) =>
            t.role === "user" ? (
              <UserTurn key={i} content={t.content} />
            ) : (
              <AssistantTurn key={i} turn={t} sessionId={sessionId} onClarify={onClarify} />
            ),
          )}
        </section>
      )}

      {loading && turns.length > 0 && (
        <p className="code mt-7" style={{ fontSize: "0.74rem", color: "var(--ink-faint)" }}>
          Consulting the corpus…
        </p>
      )}

      {error && (
        <div className="stamp mt-9" style={{ borderColor: "var(--oxblood)" }}>
          <div className="stamp__tag">Request failed</div>
          <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
            {error}
          </p>
        </div>
      )}
    </div>
  );
}

function UserTurn({ content }: { content: string }) {
  return (
    <div className="mt-9 rise">
      <div className="kicker" style={{ color: "var(--ink-faint)" }}>
        Inquiry
      </div>
      <p className="display" style={{ fontWeight: 400, fontSize: "1.25rem", lineHeight: 1.4, margin: "0.4rem 0 0" }}>
        {content}
      </p>
    </div>
  );
}

function AssistantTurn({
  turn,
  sessionId,
  onClarify,
}: {
  turn: Turn;
  sessionId: string | null;
  onClarify: (opt: ClarifyOption) => void;
}) {
  if (turn.status === "clarify") {
    return (
      <section className="mt-6 rise">
        <div className="kicker" style={{ color: "var(--gold-ink)" }}>
          Clarification requested
        </div>
        <p
          className="display"
          style={{ fontWeight: 400, fontSize: "1.3rem", lineHeight: 1.4, margin: "0.6rem 0 1.3rem" }}
        >
          {turn.interpretation || turn.content}
        </p>
        {/* Options exist only on live turns; rehydrated history shows the prompt alone. */}
        {turn.clarify.length > 0 && (
          <div className="flex flex-col gap-2.5">
            {turn.clarify.map((opt, i) => (
              <button key={`${opt.query}::${i}`} className="opt" onClick={() => onClarify(opt)}>
                <span className="opt__no">{String(i + 1).padStart(2, "0")}</span>
                <span>{opt.label}</span>
                <span className="opt__arrow" aria-hidden>
                  →
                </span>
              </button>
            ))}
          </div>
        )}
      </section>
    );
  }

  if (turn.status === "scope_warning") {
    return (
      <section className="mt-6 rise">
        <div className="stamp doc--seal">
          <div className="stamp__tag">Out of scope</div>
          <p style={{ margin: "0.5rem 0 0", fontSize: "1.02rem", lineHeight: 1.55 }}>{turn.content}</p>
        </div>
        {turn.meta && (
          <p className="code mt-3" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            audit #{turn.meta.audit_id} · {turn.meta.model_name}
          </p>
        )}
      </section>
    );
  }

  if (turn.refused) {
    return (
      <section className="mt-6 rise">
        <div className="stamp doc--seal">
          <div className="stamp__tag">Declined · not in corpus</div>
          <p style={{ margin: "0.5rem 0 0", fontSize: "1.02rem", lineHeight: 1.55 }}>{turn.content}</p>
        </div>
        {turn.meta && (
          <p className="code mt-3" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            audit #{turn.meta.audit_id} · {turn.meta.model_name}
          </p>
        )}
      </section>
    );
  }

  return (
    <section className="mt-6 rise">
      <div className="doc doc--seal doc--pad">
        <div className="kicker" style={{ color: "var(--gold-ink)", marginBottom: "0.6rem" }}>
          Finding
        </div>
        <Markdown>{turn.content}</Markdown>
      </div>

      <div className="mt-7">
        <div className="flex items-baseline gap-3">
          <h2 className="kicker" style={{ color: "var(--ink)" }}>
            References
          </h2>
          <hr className="hair grow" />
          <span className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            {turn.citations.length} cited
          </span>
        </div>

        {turn.citations.length === 0 ? (
          <p className="mt-3" style={{ color: "var(--ink-soft)", fontSize: "0.95rem" }}>
            No citations.
          </p>
        ) : (
          <div className="mt-2">
            {turn.citations.map((c, i) => (
              <Reference key={`${c.short_name}-${c.page}-${i}`} n={i + 1} c={c} />
            ))}
          </div>
        )}
      </div>

      {/* Thumbs only on live turns: meta (and so audit_id) is absent on turns
          rehydrated from session history, which degrade to no affordance. */}
      {turn.meta && <AnswerFeedback auditId={turn.meta.audit_id} />}

      {turn.meta && (
        <details className="mt-5">
          <summary className="kicker" style={{ cursor: "pointer", color: "var(--ink-faint)" }}>
            Provenance
          </summary>
          <p className="code mt-2" style={{ fontSize: "0.72rem", color: "var(--ink-soft)" }}>
            model {turn.meta.model_name} · audit #{turn.meta.audit_id} · status {turn.status}
            {sessionId ? ` · session ${sessionId}` : ""} · turn {turn.meta.turn_id}
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
            {JSON.stringify(turn.citations, null, 2)}
          </pre>
        </details>
      )}
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
