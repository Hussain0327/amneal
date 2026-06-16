"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

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

function ArrowUp() {
  return (
    <svg viewBox="0 0 20 20" width="17" height="17" aria-hidden="true" fill="none">
      <path d="M10 16V4.5M10 4.5l-4.5 4.5M10 4.5l4.5 4.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function StopGlyph() {
  return (
    <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true" fill="currentColor">
      <rect x="5" y="5" width="10" height="10" rx="1.5" />
    </svg>
  );
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
  // Polite, screen-reader-only announcement of a settled answer — the visible
  // ticker unmounts on completion, so this is the only "answer ready" cue AT
  // gets (WCAG 4.1.3). A short lead keeps it from re-reading the transcript.
  const [announcement, setAnnouncement] = useState("");
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
  // The composer relocates from top to bottom on the first send, which
  // unmounts the focused textarea; refocus it once the turn settles so the
  // keyboard never drops to <body> mid-conversation.
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const refocusRef = useRef(false);
  // The question of the in-flight run, handed back to the composer if stopped.
  const lastQuestionRef = useRef("");

  useEffect(() => {
    if (!urlSession) {
      // New chat: abort anything in flight, back to the empty state. Reset
      // historyLoading too — switching to a new chat while a prior session's
      // history fetch is in flight cancels that fetch, and its (guarded)
      // finally never clears the flag, which would otherwise wedge the UI.
      controllerRef.current?.abort();
      sessionIdRef.current = null;
      setSessionId(null);
      setTurns([]);
      setError(null);
      setHistoryLoading(false);
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

  // Restore focus to the (relocated) composer once a send settles.
  useEffect(() => {
    if (loading || !refocusRef.current) return;
    refocusRef.current = false;
    composerRef.current?.focus();
  }, [loading]);

  // One in-flight operation at a time — a live query OR a session-history
  // fetch. Every dispatch path (free-text submit, Enter, chip pick) gates on
  // this, so a turn can never be sent into a session being swapped away from.
  const busy = loading || historyLoading;

  async function run(q: string, filters: Record<string, string> | null) {
    if (busy) return; // race guard: one query or history fetch at a time
    const seq = ++runSeqRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setError(null);
    setStatusFrames([]);
    // Clear the SR live region so an identical consecutive answer/label still
    // changes the DOM text and re-announces (polite regions skip unchanged text).
    setAnnouncement("");
    scrollArmedRef.current = true;
    lastQuestionRef.current = q;
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
      // Superseded by a newer run, or aborted by a new-chat / session switch
      // (the fallback /query fetch is bound to the same signal, so an abort
      // throws above — this guards the rare in-between resolve).
      if (runSeqRef.current !== seq || controller.signal.aborted) return;
      sessionIdRef.current = next.session_id;
      setSessionId(next.session_id);
      setTurns((prev) => [...prev, assistantTurn(next)]);
      setActiveSessionId(next.session_id);
      refocusRef.current = true;
      const label =
        next.status === "clarify"
          ? "Clarification requested"
          : next.refused || next.status === "scope_warning"
            ? "Request declined — see the reply"
            : "Answer ready";
      const lead = (next.answer || next.interpretation || "").replace(/\s+/g, " ").trim().slice(0, 140);
      setAnnouncement(lead ? `${label}: ${lead}` : `${label}.`);
      if (urlSession !== next.session_id) {
        // Preserve any scoped-product params (rp/appl) when stamping the new
        // session into the URL — only `session` changes here. Read the LIVE URL
        // (not the render-time searchParams snapshot) so a product pinned DURING
        // this in-flight query isn't wiped by a stale snapshot.
        const params = new URLSearchParams(window.location.search);
        params.set("session", next.session_id);
        router.replace(`/?${params.toString()}`, { scroll: false });
      }
      void refreshSessions();
    } catch (e) {
      // An abort means new chat / session switch already took over the view.
      if (isAbortError(e) || runSeqRef.current !== seq || controller.signal.aborted) return;
      // The send failed: restore the typed question and drop the optimistic
      // inquiry turn so the composer is usable to retry (parity with main).
      setError(e instanceof Error ? e.message : String(e));
      setQuestion(q);
      setTurns((prev) => (prev.length && prev[prev.length - 1].role === "user" ? prev.slice(0, -1) : prev));
      refocusRef.current = true;
    } finally {
      if (runSeqRef.current === seq) {
        setLoading(false);
        setStatusFrames([]);
        controllerRef.current = null;
      }
    }
  }

  // Cancel an in-flight query. Aborting makes run()'s catch return early and
  // its finally clear loading/status; here we undo the optimistic inquiry turn
  // and hand the question back to the composer so it can be edited and resent.
  function stop() {
    if (!loading) return;
    controllerRef.current?.abort();
    setTurns((prev) => (prev.length && prev[prev.length - 1].role === "user" ? prev.slice(0, -1) : prev));
    // Hand the in-flight question back — but never clobber text the user has
    // started typing into the composer mid-query.
    if (lastQuestionRef.current && !question.trim()) setQuestion(lastQuestionRef.current);
    refocusRef.current = true;
  }

  function submitQuestion() {
    const q = question.trim();
    if (!q || busy) return;
    const filters: Record<string, string> = {};
    if (ingredient.trim()) filters["normalized_name"] = ingredient.trim().toLowerCase();
    if (dosage.trim()) filters["dosage_form"] = dosage.trim();
    void run(q, Object.keys(filters).length ? filters : null);
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    submitQuestion();
  }

  // Enter sends; Shift+Enter is a newline — the chat convention. Skip while an
  // IME is composing (CJK / accent dead-keys), where Enter commits the
  // candidate rather than the message.
  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submitQuestion();
    }
  }

  // Clarify options and grounded suggestions share a shape: the exact query +
  // filters to resend — click, no retyping. Sync BOTH visible filter fields so
  // a stale dosage form can't leak into the next free-text follow-up.
  function onPick(opt: Suggestion) {
    if (busy) return;
    setIngredient(opt.filters?.normalized_name ?? "");
    setDosage(opt.filters?.dosage_form ?? "");
    void run(opt.query, opt.filters ?? null);
  }

  const hasThread = turns.length > 0 || loading || historyLoading;
  // Free-text clarify: when the last assistant turn asked for clarification,
  // the composer becomes a reply — the backend resolves typed answers against
  // the pending options, so picking a card and typing are both first-class.
  const lastAssistant = [...turns].reverse().find((t) => t.role === "assistant");
  const clarifyPending = !loading && lastAssistant?.status === "clarify";
  // Restored history clarify turns carry no options (clarify: []), so the
  // "pick an option above" hint would point at nothing — only show it when
  // options are actually on screen.
  const clarifyHasOptions = clarifyPending && (lastAssistant?.clarify.length ?? 0) > 0;

  const filtersActive = [ingredient.trim(), dosage.trim()].filter(Boolean).length;
  const composer = (
    <div className="composer">
      {error && (
        <p className="composer__error code" role="alert">
          {error}
        </p>
      )}
      <form onSubmit={onSubmit}>
        {/* The ingredient/dosage filters live on, just folded away — the clarify
            flow still sets them, and a manual pre-filter is one click open. */}
        <details className="composer__filters">
          <summary className="kicker">Filters{filtersActive ? ` · ${filtersActive} active` : ""}</summary>
          <div className="composer__filters-grid">
            <input
              className="field"
              value={ingredient}
              onChange={(e) => setIngredient(e.target.value)}
              placeholder="Active ingredient · e.g. albuterol sulfate"
              aria-label="Active ingredient filter"
            />
            <input
              className="field"
              value={dosage}
              onChange={(e) => setDosage(e.target.value)}
              placeholder="Dosage form · e.g. inhalation aerosol"
              aria-label="Dosage form filter"
            />
          </div>
        </details>
        <div className="composer__bar">
          <textarea
            id="q"
            ref={composerRef}
            className="composer__input"
            rows={1}
            placeholder={
              clarifyHasOptions
                ? "Pick an option above, or reply in your own words…"
                : clarifyPending
                  ? "Reply in your own words…"
                  : "Ask about an FDA guidance, product, or change…"
            }
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={onKeyDown}
            aria-label={clarifyPending ? "Reply" : "Ask the guidance corpus"}
          />
          {loading ? (
            <button className="composer__send" type="button" onClick={stop} aria-label="Stop generating">
              <StopGlyph />
            </button>
          ) : (
            <button
              className="composer__send"
              type="submit"
              disabled={busy || !question.trim()}
              aria-label={clarifyPending ? "Send reply" : "Submit inquiry"}
            >
              <ArrowUp />
            </button>
          )}
        </div>
      </form>
    </div>
  );

  return (
    <div className="chat">
      <header className="chat__head rise">
        <h1 className="kicker" style={{ color: "var(--ink-soft)", margin: 0 }}>
          01 · Ask
        </h1>
        <p className="chat__sub">
          Plain-language Q&amp;A over FDA product-specific guidance — every claim cited to its source, and if a
          question is unclear it asks rather than guesses.
        </p>
      </header>

      <div className="chat__thread">
        {!hasThread && (
          <div className="chat__empty rise d2">
            <p className="chat__empty-lead">Ask about an FDA guidance, product, or change.</p>
            <div className="chat__examples">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex.q}
                  className="pill"
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
        )}

        {historyLoading && <p className="chat__note code">Opening conversation…</p>}

        {turns.map((t, i) => {
          // Prefer a stable identity (live assistant turns carry meta.turn_id)
          // over the array index, so a turn's child state (feedback, details)
          // tracks the turn rather than its position.
          const key = `${t.role}-${t.meta?.turn_id ?? i}`;
          return t.role === "user" ? (
            <UserTurn key={key} content={t.content} />
          ) : (
            <AssistantTurn key={key} turn={t} sessionId={sessionId} onPick={onPick} busy={busy} />
          );
        })}

        {/* While the (currently blocking) /query runs, the assistant slot shows
            the docket ticker — honest motion, no faked token stream. */}
        {loading && (
          <div className="chat-row rise">
            <span className="avatar" aria-hidden>
              RW
            </span>
            <div className="msg">
              <StatusTicker frames={statusFrames} />
            </div>
          </div>
        )}

        <div ref={threadEndRef} aria-hidden />
      </div>

      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>

      {composer}
    </div>
  );
}
