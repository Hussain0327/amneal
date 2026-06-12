"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { StatusTicker } from "@/components/StatusTicker";
import { AssistantTurn, UserTurn } from "@/components/Turns";
import { useSessions } from "@/components/SessionsProvider";
import { askQueryStream, getSession, type Suggestion } from "@/lib/api";
import { assistantTurn, turnFromMessage, userTurn, type Turn } from "@/lib/turns";

const EXAMPLES = [
  { label: "albuterol BE study", q: "What BE study design is recommended for albuterol sulfate inhalation aerosol?" },
  { label: "beclomethasone aerosol", q: "What type of study does the beclomethasone dipropionate inhalation aerosol PSG recommend?" },
  { label: "propranolol", q: "propranolol" },
  { label: "metformin dissolution", q: "What dissolution method is recommended for metformin hydrochloride?" },
];

function isAbortError(e: unknown): boolean {
  return e instanceof Error && e.name === "AbortError";
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
  // SSE status frames for the in-flight query; cleared when the answer lands.
  const [statusFrames, setStatusFrames] = useState<string[]>([]);
  // Mirrors sessionId so the URL-sync effect can tell "we just created this
  // session live" (skip refetch) from "another session was selected" (fetch).
  const sessionIdRef = useRef<string | null>(null);
  // In-flight race guard: one run at a time; a session switch or new chat
  // aborts the active stream, and a stale run discovers it via the sequence.
  const runSeqRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  // Auto-scroll is armed only by live activity — never by rehydrating an old
  // conversation, which should open at the top like a document.
  const scrollArmedRef = useRef(false);
  const threadEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!urlSession) {
      // New chat: abort anything in flight, back to the empty state.
      controllerRef.current?.abort();
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
    controllerRef.current?.abort();
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

  // Abort any in-flight stream on unmount.
  useEffect(() => () => controllerRef.current?.abort(), []);

  useEffect(() => {
    if (!scrollArmedRef.current) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    threadEndRef.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "end" });
    if (!loading) scrollArmedRef.current = false;
  }, [turns, loading, statusFrames]);

  async function run(q: string, filters: Record<string, string> | null) {
    if (loading) return; // race guard: one send at a time
    const seq = ++runSeqRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setError(null);
    setStatusFrames([]);
    scrollArmedRef.current = true;
    // The inquiry joins the thread immediately; the ticker answers it in place.
    setTurns((prev) => [...prev, userTurn(q)]);
    setQuestion("");
    try {
      const next = await askQueryStream(
        q,
        filters,
        sessionIdRef.current,
        (text) => {
          if (runSeqRef.current === seq) setStatusFrames((prev) => [...prev, text]);
        },
        controller.signal,
      );
      if (runSeqRef.current !== seq) return; // superseded by a newer run
      sessionIdRef.current = next.session_id;
      setSessionId(next.session_id);
      setTurns((prev) => [...prev, assistantTurn(next)]);
      setActiveSessionId(next.session_id);
      if (urlSession !== next.session_id) {
        router.replace(`/?session=${encodeURIComponent(next.session_id)}`, { scroll: false });
      }
      void refreshSessions();
    } catch (e) {
      // An abort means new chat / session switch already took over the view.
      if (isAbortError(e) || runSeqRef.current !== seq) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (runSeqRef.current === seq) {
        setLoading(false);
        setStatusFrames([]);
        controllerRef.current = null;
      }
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

  // Clarify options and grounded suggestions share a shape: the exact query +
  // filters to resend — click, no retyping.
  function onPick(opt: Suggestion) {
    if (loading) return;
    setIngredient(opt.filters?.normalized_name ?? "");
    void run(opt.query, opt.filters ?? null);
  }

  const hasThread = turns.length > 0 || loading || historyLoading;
  // Free-text clarify: when the last assistant turn asked for clarification,
  // the composer becomes a reply — the backend resolves typed answers against
  // the pending options, so picking a card and typing are both first-class.
  const lastAssistant = [...turns].reverse().find((t) => t.role === "assistant");
  const clarifyPending = !loading && lastAssistant?.status === "clarify";

  const composer = (
    <form onSubmit={onSubmit} className={hasThread ? "mt-10" : "rise d3"}>
      <label className="kicker" htmlFor="q" style={{ color: clarifyPending ? "var(--gold-ink)" : "var(--ink-soft)" }}>
        {clarifyPending ? "Reply" : hasThread ? "Follow-up inquiry" : "Inquiry"}
      </label>
      <textarea
        id="q"
        className="field field--inquiry"
        style={{ marginTop: "0.5rem", minHeight: "3.4rem" }}
        placeholder={
          clarifyPending
            ? "Pick an option above, or reply in your own words"
            : "propranolol   ·   What BE study design is recommended for metformin?"
        }
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
          {loading ? "Consulting…" : clarifyPending ? "Send reply" : "Submit inquiry"}
        </button>
      </div>
    </form>
  );

  return (
    <div className="measure">
      <PageHeader
        index="01"
        product="Ask"
        title="Ask the guidance corpus."
        tagline="Plain-language Q&A over FDA product-specific guidance. Every claim is cited to its source — and if a question is unclear, it asks rather than guesses."
      />

      {/* Empty state: the inquiry desk up top, examples beneath. Once a thread
          exists, the composer moves below the latest turn — you sign the next
          line of the correspondence. */}
      {!hasThread && (
        <>
          {composer}
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
        </>
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
              <AssistantTurn key={i} turn={t} sessionId={sessionId} onPick={onPick} busy={loading} />
            ),
          )}
        </section>
      )}

      {loading && <StatusTicker frames={statusFrames} />}

      {error && (
        <div className="stamp mt-9" style={{ borderColor: "var(--oxblood)" }}>
          <div className="stamp__tag">Request failed</div>
          <p className="code mt-1" style={{ fontSize: "0.82rem" }}>
            {error}
          </p>
        </div>
      )}

      {hasThread && !historyLoading && composer}
      <div ref={threadEndRef} aria-hidden />
    </div>
  );
}
