"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

import { EvidenceDrawer } from "@/components/EvidenceDrawer";
import { StatusTicker } from "@/components/StatusTicker";
import { AssistantTurn, ProvisionalDraft, UserTurn } from "@/components/Turns";
import { useSessions } from "@/components/SessionsProvider";
import { useSettings } from "@/components/SettingsProvider";
import { askQueryStream, getSession, STREAM_FALLBACK_STATUS, type Citation, type Suggestion } from "@/lib/api";
import type { SessionMeta } from "@/lib/auth-types";
import { formatFiled, parseApiDate } from "@/lib/time";
import { assistantTurn, nonAnswerLabel, turnFromMessage, userTurn, type Turn } from "@/lib/turns";
import { syncTextareaHeight } from "@/lib/composer";

// Example inquiries grouped by the KIND of question the corpus answers — the
// grouping teaches the tool's range (a study-design question, a spec question, or
// just a product name it will scope for you), not decoration.
const EXAMPLE_GROUPS = [
  {
    kind: "Study design",
    items: [
      { label: "BE study for albuterol sulfate inhalation aerosol", q: "What BE study design is recommended for albuterol sulfate inhalation aerosol?" },
      { label: "Beclomethasone dipropionate aerosol study type", q: "What type of study does the beclomethasone dipropionate inhalation aerosol PSG recommend?" },
    ],
  },
  {
    kind: "Dissolution & specs",
    items: [
      { label: "Dissolution method for metformin hydrochloride", q: "What dissolution method is recommended for metformin hydrochloride?" },
    ],
  },
  {
    kind: "Just a product name",
    items: [{ label: "propranolol", q: "propranolol" }],
  },
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

function CrossGlyph() {
  return (
    <svg viewBox="0 0 20 20" width="11" height="11" aria-hidden="true" fill="none">
      <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

// Human labels for the two structured filter fields; clarify-pick extras
// (e.g. "route") read as their key with underscores spaced.
const FILTER_KEY_LABELS: Record<string, string> = {
  normalized_name: "ingredient",
  dosage_form: "form",
};

function filterLabel(key: string): string {
  return FILTER_KEY_LABELS[key] ?? key.replace(/_/g, " ");
}

// Enough of the recoverable question to recognize it, not a transcript.
function truncateQuote(q: string): string {
  return q.length > 60 ? `${q.slice(0, 60).trimEnd()}\u2026` : q;
}

// A send that reached the catch path: everything needed to retry it verbatim.
interface FailedSend {
  readonly question: string;
  readonly filters: Record<string, string> | null;
  readonly message: string;
}

// Active-scope chips between the (folded) filter editor and the composer bar:
// the details element hid the scope, so a clarify pick could silently scope
// every follow-up. One quiet chip per active filter; the cross clears it.
function ScopeChips({
  entries,
  onClear,
}: {
  entries: ReadonlyArray<readonly [string, string]>;
  onClear: (key: string) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <div className="composer__scope">
      {entries.map(([key, value]) => (
        <button
          key={key}
          type="button"
          className="chip composer__scope-chip"
          aria-label={`Clear filter: ${filterLabel(key)} ${value}`}
          onClick={() => onClear(key)}
        >
          <span className="chip__k">{filterLabel(key)}</span>
          {value}
          <CrossGlyph />
        </button>
      ))}
    </div>
  );
}

// Transport-register failure under the unanswered inquiry turn: quiet mono
// oxblood TEXT, deliberately NOT the declined block -- that register is an
// epistemic verdict (INV-2); this is plumbing, so no wash/seam/tag.
function SendFailNotice({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="sendfail" role="alert">
      <p className="sendfail__msg code">{`Not sent \u2014 ${message}`}</p>
      <button type="button" className="sendfail__retry" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

// Client-side typewriter timing for the live-draft channel (onDraft). Adaptive
// drain, not a fixed rate: base cadence while caught up, and once the pending
// buffer crosses the catch-up threshold the per-tick take scales up so the
// visible text is never more than roughly one catch-up window behind the
// wire -- hungry when behind, calm once caught up.
const DRAFT_TICK_MS = 33; // ~30fps
const DRAFT_CHARS_PER_TICK = 6; // base cadence (~180 chars/s) once caught up
const DRAFT_CATCHUP_THRESHOLD_CHARS = 400;
const DRAFT_CATCHUP_WINDOW_MS = 1000;

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
  // Grounds each answer's confidence tooltip in the live refusal threshold;
  // null until /settings resolves (the tooltip degrades, never fakes a number).
  const { settings } = useSettings();

  const [question, setQuestion] = useState("");
  const [ingredient, setIngredient] = useState("");
  const [dosage, setDosage] = useState("");
  // Clarify-pick filter keys beyond the two visible fields (e.g. "route").
  // Persisted so a pick's scope truthfully applies to typed follow-ups too --
  // previously these applied to the pick and then silently vanished.
  const [extraFilters, setExtraFilters] = useState<Record<string, string>>({});
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  // The reopened conversation's server identity (title + opened date), rendered
  // as a docket header above the transcript. null on a live-only conversation
  // -- the server names a session asynchronously, so we never fake a title.
  const [sessionMeta, setSessionMeta] = useState<SessionMeta | null>(null);
  const [loading, setLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  // History-load failures ONLY (rendered above the composer). Send failures
  // live in-thread as failedSend so the typed question is never thrown away.
  const [error, setError] = useState<string | null>(null);
  // The last send that failed in run()'s catch: its optimistic turn stays in
  // the thread and a Retry renders under it. Mirrored in a ref so the shared
  // clear can run inside the URL-sync effect without becoming a dependency.
  const [failedSend, setFailedSend] = useState<FailedSend | null>(null);
  const failedSendRef = useRef<FailedSend | null>(null);
  // A question cancelled mid-flight (stop / session switch / new chat) that
  // can be handed back to an EMPTY composer via the restore chip.
  const [recoverableQuestion, setRecoverableQuestion] = useState<string | null>(null);
  // SSE status frames for the in-flight query; cleared when the answer lands.
  const [statusFrames, setStatusFrames] = useState<string[]>([]);
  // Provisional answer text streamed token-by-token BEFORE citation validation.
  // Rendered as a clearly-provisional "draft" (no citations/drawer/feedback); the
  // validated turn replaces it on the result frame. Reset alongside statusFrames.
  const [draft, setDraft] = useState<string | null>(null);
  // Client-side typewriter for the LIVE draft channel (onDraft): incoming
  // deltas land in this buffer and a paced interval drains them into `draft`,
  // so render cadence stays smooth regardless of wire chunking -- the server
  // sends deltas as fast as the model writes, which can arrive in bursts.
  const draftBufRef = useRef("");
  const draftTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopDraftDrain = useCallback((discardBuffered: boolean) => {
    if (draftTimerRef.current !== null) {
      clearInterval(draftTimerRef.current);
      draftTimerRef.current = null;
    }
    if (discardBuffered) draftBufRef.current = "";
  }, []);

  const ensureDraftDrain = useCallback(() => {
    if (draftTimerRef.current !== null) return;
    draftTimerRef.current = setInterval(() => {
      const buf = draftBufRef.current;
      if (!buf) {
        stopDraftDrain(false);
        return;
      }
      // Adaptive take: base rate while caught up; once the backlog crosses
      // the threshold, take enough per tick to clear it within the catch-up
      // window instead of falling further behind.
      const ticksToClear = Math.max(1, Math.round(DRAFT_CATCHUP_WINDOW_MS / DRAFT_TICK_MS));
      const take =
        buf.length > DRAFT_CATCHUP_THRESHOLD_CHARS
          ? Math.max(DRAFT_CHARS_PER_TICK, Math.ceil(buf.length / ticksToClear))
          : DRAFT_CHARS_PER_TICK;
      draftBufRef.current = buf.slice(take);
      setDraft((prev) => (prev ?? "") + buf.slice(0, take));
    }, DRAFT_TICK_MS);
  }, [stopDraftDrain]);

  // Single entry point for a live-draft delta: reduced motion skips pacing
  // entirely (the whole buffer flushes immediately) but still goes through
  // this buffer, so there is exactly one code path either way.
  const pushDraftDelta = useCallback(
    (delta: string) => {
      draftBufRef.current += delta;
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        const buf = draftBufRef.current;
        draftBufRef.current = "";
        setDraft((prev) => (prev ?? "") + buf);
        return;
      }
      ensureDraftDrain();
    },
    [ensureDraftDrain],
  );
  // Polite, screen-reader-only announcement of a settled answer — the visible
  // ticker unmounts on completion, so this is the only "answer ready" cue AT
  // gets (WCAG 4.1.3). A short lead keeps it from re-reading the transcript.
  const [announcement, setAnnouncement] = useState("");
  // Second polite region, for composer-side state changes (programmatic
  // scoping, pill filter clears, question recovery): the answer region above
  // must stay reserved for answer lifecycle or the two would overwrite each
  // other mid-stream. Cleared alongside announcement at run() start.
  const [composerNotice, setComposerNotice] = useState("");
  // The one write path for composer notices. flushSync is load-bearing: it
  // commits the CLEAR as its own DOM update before the text lands. Without it
  // "clear + set the same text" coalesce into one React batch -- a no-op DOM
  // diff -- and polite live regions only announce on an actual text change, so
  // a second identical notice would be silent to a screen reader.
  const announceComposer = useCallback((text: string) => {
    flushSync(() => setComposerNotice(""));
    setComposerNotice(text);
  }, []);
  // The citation whose evidence drawer is open (null = closed). Presentation-only
  // over an already-validated citation; closeDrawer is stable so the drawer's
  // focus effect doesn't re-run on every render.
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);
  const closeDrawer = useCallback(() => setActiveCitation(null), []);
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

  // A failed send's turn is client-only (the server never saw it): before any
  // NEXT dispatch or session reset it must leave the thread, or the transcript
  // shows an unanswered question that looks server-persisted. Stable identity
  // (ref-backed) so the URL-sync effect can call it without re-running.
  const clearFailedSend = useCallback(() => {
    if (!failedSendRef.current) return;
    failedSendRef.current = null;
    setFailedSend(null);
    setTurns((prev) =>
      prev.length && prev[prev.length - 1].role === "user" ? prev.slice(0, -1) : prev,
    );
  }, []);

  // This effect synchronizes local chat state to the URL `session` param: every
  // setState in it is an intentional reset/sync (new chat, switch, or load), not
  // a cascading-render bug. set-state-in-effect is a new rule in
  // eslint-config-next 16's react-hooks plugin; disable it for the whole effect.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!urlSession) {
      // New chat: abort anything in flight, back to the empty state. Reset
      // historyLoading too — switching to a new chat while a prior session's
      // history fetch is in flight cancels that fetch, and its (guarded)
      // finally never clears the flag, which would otherwise wedge the UI.
      const wasStreaming = controllerRef.current !== null;
      controllerRef.current?.abort();
      sessionIdRef.current = null;
      setSessionId(null);
      setSessionMeta(null);
      setTurns([]);
      setError(null);
      setHistoryLoading(false);
      // A cancelled load's "Opening conversation" notice must not outlive the
      // load it described.
      setComposerNotice("");
      setActiveSessionId(null);
      // A failed send's question is typed work the server never saw: park it
      // behind the restore chip BEFORE clearFailedSend tears down its Retry
      // notice -- clearing alone would discard the question silently.
      if (failedSendRef.current) {
        setRecoverableQuestion(failedSendRef.current.question);
      }
      clearFailedSend();
      // A question cut off by starting a new chat is handed back as a restore
      // chip rather than silently discarded (typed work is never lost).
      if (wasStreaming && lastQuestionRef.current) {
        setRecoverableQuestion(lastQuestionRef.current);
      }
      // A new chat opens unscoped: filters left behind by the previous
      // conversation's clarify pick (or typed by hand) must not silently scope
      // its first question — the starter pills already clear these; this covers
      // the typed-question path. The evidence drawer closes for the same
      // reason: its citation belongs to the conversation being left.
      setIngredient("");
      setDosage("");
      setExtraFilters({});
      setActiveCitation(null);
      return;
    }
    if (urlSession === sessionIdRef.current) {
      // Back on the session we already hold. A prior run's history fetch may have
      // been cancelled by the cleanup below, and its (guarded) finally then never
      // clears the flag -- reset it here, same as the new-chat branch above, or
      // the composer stays disabled with no Stop button to escape it.
      setHistoryLoading(false);
      // ...and the cancelled load's notice leaves with the flag.
      setComposerNotice("");
      setActiveSessionId(urlSession);
      return;
    }
    // A query in flight belongs to the session being left. Aborting makes run()'s
    // catch return early WITHOUT undoing the optimistic inquiry turn (only stop()
    // does that), so drop it here too: otherwise coming back to this session shows
    // a dangling unanswered question while the server has already persisted both
    // it and its answer. The question is NOT typed back into the composer the
    // way stop() does -- the analyst navigated away -- but it is offered back
    // as a restore chip instead of being silently discarded. (The server may
    // still persist the interrupted turn into the session being left.)
    const wasStreaming = controllerRef.current !== null;
    controllerRef.current?.abort();
    if (wasStreaming) {
      setTurns((prev) =>
        prev.length && prev[prev.length - 1].role === "user" ? prev.slice(0, -1) : prev,
      );
      if (lastQuestionRef.current) setRecoverableQuestion(lastQuestionRef.current);
    }
    // Same as the new-chat branch: a failed send's question survives the
    // switch behind the restore chip instead of vanishing with its notice.
    if (failedSendRef.current) {
      setRecoverableQuestion(failedSendRef.current.question);
    }
    clearFailedSend();
    // Close the drawer before swapping threads: browser back/forward changes
    // ?session without a click (the scrim only blocks in-page clicks), and the
    // previous conversation's evidence must not float over the next one.
    setActiveCitation(null);
    let cancelled = false;
    setHistoryLoading(true);
    setError(null);
    // The role=status hint inserted under the composer is fresh DOM, which
    // VoiceOver often fails to announce; the persistent composer notice
    // region is the reliable channel, so state the load there too. Set
    // directly (NOT announceComposer): flushSync may not run inside an
    // effect, and this path never needs the re-announce trick — the region
    // is always empty here (cleared by every prior settle/reset).
    setComposerNotice("Opening conversation");
    getSession(urlSession)
      .then((d) => {
        if (cancelled) return;
        sessionIdRef.current = urlSession;
        setSessionId(urlSession);
        setSessionMeta(d.session);
        setTurns(d.messages.map(turnFromMessage));
        setActiveSessionId(urlSession);
        void refreshSessions();
      })
      .catch((e) => {
        if (cancelled) return;
        // The switch failed: leaving identity at the PREVIOUS session would
        // silently append the next question (threading that session's memory)
        // into a conversation the user is no longer looking at — and then
        // teleport the URL back to it. Reset so a send after the error starts
        // a fresh session instead.
        sessionIdRef.current = null;
        setSessionId(null);
        setSessionMeta(null);
        setActiveSessionId(null);
        setTurns([]);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) {
          setHistoryLoading(false);
          // The load settled either way; its notice must not linger as if a
          // conversation were still opening.
          setComposerNotice("");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [urlSession, refreshSessions, setActiveSessionId, clearFailedSend, announceComposer]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // Abort any in-flight stream on unmount, and release the draft pacing
  // timer with it -- otherwise a still-buffered draft would keep ticking
  // setDraft on an unmounted component.
  useEffect(() => () => {
    controllerRef.current?.abort();
    stopDraftDrain(true);
  }, [stopDraftDrain]);

  // `draft` is a dependency so the view keeps following the answer while it
  // streams token-by-token — status frames stop once the first token arrives,
  // and turns don't change until the validated result lands.
  useEffect(() => {
    if (!scrollArmedRef.current) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    threadEndRef.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "end" });
    if (!loading) scrollArmedRef.current = false;
  }, [turns, loading, statusFrames, draft]);

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

  // useCallback (with onPick below) keeps handler identity stable across the
  // per-token re-renders of a streaming answer, so the React.memo on the turn
  // components actually bails out — settled turns must not re-parse their
  // markdown once per SSE chunk.
  const run = useCallback(
    async (q: string, filters: Record<string, string> | null) => {
      if (busy) return; // race guard: one query or history fetch at a time
      const seq = ++runSeqRef.current;
      const controller = new AbortController();
      controllerRef.current = controller;
      setLoading(true);
      setError(null);
      setStatusFrames([]);
      setDraft(null);
      stopDraftDrain(true);
      // Clear BOTH SR live regions so an identical consecutive answer/label
      // still changes the DOM text and re-announces (polite regions skip
      // unchanged text), and a stale composer notice can't outlive the state
      // it described. A cancelled question is superseded by this new send.
      setAnnouncement("");
      setComposerNotice("");
      setRecoverableQuestion(null);
      scrollArmedRef.current = true;
      lastQuestionRef.current = q;
      // The SSE frames this run settles through, accumulated in the closure:
      // the statusFrames STATE is wiped in finally before the settled turn
      // renders, so this array is the only record that can be persisted onto
      // the turn (provenance status log + fallback notice).
      const frames: string[] = [];
      let fellBack = false;
      // Drafting-milestone throttle for the SR region: the draft row is
      // aria-hidden, so without these a screen-reader user hears nothing
      // between the ticker and the settle announcement. Milestones repeat no
      // sooner than every 15s, and each string differs (a counter suffix) --
      // polite regions skip textually-unchanged content.
      let milestoneAt = 0;
      let milestoneNo = 0;
      // Shared by onToken and onDraft (whichever lands first): one counter,
      // so a turn that streams both a live draft and the post-audit replay
      // still announces the milestone exactly once per 15s window.
      const announceDraftMilestone = () => {
        const now = Date.now();
        if (milestoneNo === 0) {
          milestoneNo = 1;
          milestoneAt = now;
          setAnnouncement("Drafting a provisional answer \u2014 the verified answer will follow.");
        } else if (now - milestoneAt >= 15000) {
          milestoneNo += 1;
          milestoneAt = now;
          setAnnouncement(`Still drafting \u2014 update ${milestoneNo}`);
        }
      };
      // The inquiry joins the thread immediately; the ticker answers it in place.
      setTurns((prev) => [...prev, userTurn(q)]);
      setQuestion("");
      try {
        const next = await askQueryStream(
          q,
          filters,
          sessionIdRef.current,
          {
            onStatus: (text) => {
              if (runSeqRef.current !== seq) return;
              frames.push(text);
              // The api layer emits this exact status immediately before every
              // stream-failure fallback to plain /query: the dead stream's
              // provisional tokens are contractually discarded, so drop the
              // draft and let the ticker (showing this retry line) take the
              // in-flight slot back for the re-run.
              if (text === STREAM_FALLBACK_STATUS) {
                fellBack = true;
                setDraft(null);
                stopDraftDrain(true);
              }
              setStatusFrames((prev) => [...prev, text]);
            },
            onToken: (delta) => {
              if (runSeqRef.current !== seq) return;
              setDraft((prev) => (prev ?? "") + delta);
              announceDraftMilestone();
            },
            onDraft: (delta) => {
              if (runSeqRef.current !== seq) return;
              pushDraftDelta(delta);
              announceDraftMilestone();
            },
            onDraftReset: () => {
              if (runSeqRef.current !== seq) return;
              stopDraftDrain(true);
              setDraft(null);
            },
          },
          true,
          controller.signal,
        );
        // Superseded by a newer run, or aborted by a new-chat / session switch
        // (the fallback /query fetch is bound to the same signal, so an abort
        // throws above — this guards the rare in-between resolve).
        if (runSeqRef.current !== seq || controller.signal.aborted) return;
        sessionIdRef.current = next.session_id;
        setSessionId(next.session_id);
        // Swap the provisional draft for the validated turn in one render batch.
        setDraft(null);
        stopDraftDrain(true);
        setTurns((prev) => [
          ...prev,
          assistantTurn(next, {
            statusLog: frames,
            streamFellBack: fellBack,
            draftWithdrawn: next.draft_withdrawn ?? null,
          }),
        ]);
        setActiveSessionId(next.session_id);
        refocusRef.current = true;
        const nonAnswer = nonAnswerLabel(next.status, next.refused, next.reason ?? null);
        // Count-aware clarify arm: the options render ABOVE the composer the
        // screen-reader user is focused in, so this announcement is their only
        // discovery mechanism. Legacy zero-option clarifies stay plain.
        const clarifyCount = next.clarify.length;
        const label =
          next.status === "clarify"
            ? clarifyCount > 0
              ? `Clarification requested \u2014 ${clarifyCount} option${
                  clarifyCount === 1 ? "" : "s"
                } to pick from, above the reply box`
              : "Clarification requested"
            : nonAnswer
              ? `${nonAnswer} — see the reply`
              : next.status === "meta"
                ? "Information ready"
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
        // The send failed: the optimistic inquiry turn STAYS in the thread and
        // the failure renders under it with a Retry (transport register), so
        // the typed question keeps its place instead of bouncing back to the
        // composer. clearFailedSend pops the client-only turn before any next
        // dispatch or session reset.
        const failed: FailedSend = {
          question: q,
          filters,
          message: e instanceof Error ? e.message : String(e),
        };
        failedSendRef.current = failed;
        setFailedSend(failed);
        refocusRef.current = true;
      } finally {
        if (runSeqRef.current === seq) {
          setLoading(false);
          setStatusFrames([]);
          setDraft(null);
          stopDraftDrain(true);
          controllerRef.current = null;
        }
      }
    },
    [busy, urlSession, router, refreshSessions, setActiveSessionId, stopDraftDrain, pushDraftDelta],
  );

  // Cancel an in-flight query. Aborting makes run()'s catch return early and
  // its finally clear loading/status; here we undo the optimistic inquiry turn
  // and hand the question back to the composer so it can be edited and resent.
  function stop() {
    if (!loading) return;
    controllerRef.current?.abort();
    setDraft(null);
    stopDraftDrain(true);
    setTurns((prev) => (prev.length && prev[prev.length - 1].role === "user" ? prev.slice(0, -1) : prev));
    // Hand the in-flight question back — but never clobber text the user has
    // started typing into the composer mid-query: that text wins the composer,
    // and the cancelled question is parked behind a restore chip instead.
    if (lastQuestionRef.current && !question.trim()) {
      setQuestion(lastQuestionRef.current);
    } else if (lastQuestionRef.current) {
      setRecoverableQuestion(lastQuestionRef.current);
      announceComposer("Stopped \u2014 the cancelled question can be restored below.");
    }
    refocusRef.current = true;
  }

  function submitQuestion() {
    const q = question.trim();
    if (!q || busy) return;
    clearFailedSend();
    // extraFilters first so the two visible fields stay authoritative for
    // their own keys (a pick never writes those keys into extras anyway).
    const filters: Record<string, string> = { ...extraFilters };
    if (ingredient.trim()) filters["normalized_name"] = ingredient.trim().toLowerCase();
    if (dosage.trim()) filters["dosage_form"] = dosage.trim();
    void run(q, Object.keys(filters).length ? filters : null);
  }

  // The single dispatch path for starter pills: examples are authored to run
  // unscoped, so any leftover scope is cleared FIRST and the clear announced
  // -- previously pills bypassed the filters entirely while typed sends
  // honored them (two dispatch semantics).
  function sendStarter(q: string) {
    if (busy) return;
    clearFailedSend();
    const hadScope =
      Boolean(ingredient.trim()) || Boolean(dosage.trim()) || Object.keys(extraFilters).length > 0;
    if (hadScope) {
      setIngredient("");
      setDosage("");
      setExtraFilters({});
    }
    void run(q, null);
    // After run() so its prelude clear is flushed away first; announceComposer
    // then commits its own clear + set, so even an identical consecutive
    // notice re-announces.
    if (hadScope) {
      announceComposer("Filters cleared \u2014 example questions run unscoped.");
    }
  }

  // Re-fires the failed send verbatim (same question AND filters). run()
  // re-appends the inquiry turn, so the stale one is popped via the shared
  // clear first -- exactly one user row ever exists for the retried question.
  function retryFailedSend() {
    const failed = failedSendRef.current;
    if (!failed || busy) return;
    clearFailedSend();
    void run(failed.question, failed.filters);
    // The Retry button unmounts the moment loading renders (the ticker takes
    // the slot back), which would drop focus to <body>; park it in the
    // composer so keyboard users keep their place.
    composerRef.current?.focus();
  }

  // Restore-chip click: fills ONLY an empty composer; typed text always wins.
  function restoreQuestion() {
    const q = recoverableQuestion;
    if (!q) return;
    if (question.trim()) {
      announceComposer(
        "Clear the composer first \u2014 restoring will not overwrite typed text.",
      );
      return;
    }
    setQuestion(q);
    setRecoverableQuestion(null);
    composerRef.current?.focus();
  }

  // Scope-chip clear: the two structured keys empty their visible fields;
  // clarify-pick extras leave the persisted map.
  function clearFilter(key: string) {
    if (key === "normalized_name") {
      setIngredient("");
    } else if (key === "dosage_form") {
      setDosage("");
    } else {
      setExtraFilters((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
    // The clicked chip unmounts with its filter, which would drop focus to
    // <body>; the composer is where a scope edit lands next.
    composerRef.current?.focus();
  }

  function onComposerChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setQuestion(e.target.value);
    // The composer error is history-load-only now; the first edit after it
    // signals "moving on", so it stops shouting over the new question.
    if (error) setError(null);
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    submitQuestion();
  }

  // Auto-grow the composer to its CSS max-height (9rem), then let overflow
  // scroll. field-sizing:content handles this natively where supported; this
  // scrollHeight sync is the fallback for browsers that lack it. Reset to auto
  // first so the textarea can also SHRINK when text is deleted.
  function autoGrow(e: React.FormEvent<HTMLTextAreaElement>) {
    syncTextareaHeight(e.currentTarget);
  }
  // Resync on programmatic value changes (cleared after send, refilled on stop)
  // — those don't fire onInput, so the height would otherwise stick.
  useEffect(() => {
    syncTextareaHeight(composerRef.current);
  }, [question]);

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
  // Stable (useCallback) because it is a prop of the memoized AssistantTurn:
  // an unstable identity would bust the memo on every streamed token.
  const onPick = useCallback(
    (opt: Suggestion) => {
      if (busy) return;
      clearFailedSend();
      // Clarify filters are typed Record<string, unknown> (the backend models
      // them as dict[str, Any]); they are deterministic string maps in practice,
      // so narrow them for the outbound request + the visible filter fields.
      const filters = (opt.filters ?? null) as Record<string, string> | null;
      setIngredient(filters?.normalized_name ?? "");
      setDosage(filters?.dosage_form ?? "");
      // Keys beyond the two visible fields (e.g. "route") persist into
      // extraFilters so typed follow-ups stay scoped the way the pick was --
      // the scope chips then state that truthfully. Only string values ride
      // along; anything else is not a filter we can echo or resend.
      const extras: Record<string, string> = {};
      for (const [k, v] of Object.entries(opt.filters ?? {})) {
        if (k === "normalized_name" || k === "dosage_form") continue;
        if (typeof v === "string" && v) extras[k] = v;
      }
      setExtraFilters(extras);
      // The pick request itself still sends opt.filters verbatim.
      void run(opt.query, filters);
      // Announce the programmatic scoping AFTER run(): its prelude clears the
      // notice region, and within this event's batch the last write wins.
      const scoped: string[] = [];
      if (filters?.normalized_name) scoped.push(`${filterLabel("normalized_name")} ${filters.normalized_name}`);
      if (filters?.dosage_form) scoped.push(`${filterLabel("dosage_form")} ${filters.dosage_form}`);
      for (const [k, v] of Object.entries(extras)) scoped.push(`${filterLabel(k)} ${v}`);
      if (scoped.length > 0) {
        announceComposer(`Search scoped to ${scoped.join(", ")}.`);
      }
    },
    [busy, run, clearFailedSend, announceComposer],
  );

  const hasThread = turns.length > 0 || loading || historyLoading;
  // Free-text clarify: when the last assistant turn asked for clarification,
  // the composer becomes a reply — the backend resolves typed answers against
  // the pending options, so picking a card and typing are both first-class.
  const lastAssistant = [...turns].reverse().find((t) => t.role === "assistant");
  const clarifyPending = !loading && lastAssistant?.status === "clarify";
  // Clarify options ARE persisted (Tier-2), so restored history keeps them;
  // pre-Tier-2 legacy rows rehydrate with clarify: [], where the "pick an
  // option above" hint would point at nothing — keep it gated on options
  // actually being on screen.
  const clarifyHasOptions = clarifyPending && (lastAssistant?.clarify.length ?? 0) > 0;

  // Every filter the NEXT typed send will carry, in render order: the two
  // structured fields, then clarify-pick extras. Drives both the chips and
  // the honest "N active" counter (extras used to be invisible).
  const activeFilterEntries: Array<[string, string]> = [];
  // Chips state the value exactly as submitQuestion will SEND it: the
  // ingredient is lowercased on the wire, so the chip lowercases too --
  // showing the raw-cased field would misstate the outgoing scope.
  if (ingredient.trim()) {
    activeFilterEntries.push(["normalized_name", ingredient.trim().toLowerCase()]);
  }
  if (dosage.trim()) activeFilterEntries.push(["dosage_form", dosage.trim()]);
  for (const [k, v] of Object.entries(extraFilters)) activeFilterEntries.push([k, v]);
  const filtersActive = activeFilterEntries.length;
  const composer = (
    <div className="composer">
      {error && (
        <p className="composer__error code" role="alert">
          {error}
        </p>
      )}
      {recoverableQuestion && !loading && (
        <button type="button" className="chip composer__restore" onClick={restoreQuestion}>
          Restore question
          <span className="composer__restore-quote">{truncateQuote(recoverableQuestion)}</span>
        </button>
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
        <ScopeChips entries={activeFilterEntries} onClear={clearFilter} />
        <div className="composer__bar">
          <textarea
            id="q"
            ref={composerRef}
            className="composer__input"
            rows={1}
            placeholder={
              historyLoading
                ? "Opening conversation\u2026"
                : clarifyHasOptions
                  ? "Pick an option above, or reply in your own words\u2026"
                  : clarifyPending
                    ? "Reply in your own words\u2026"
                    : turns.length > 0
                      ? "Ask a follow-up, or start a new question\u2026"
                      : "Ask about an FDA guidance, product, or change\u2026"
            }
            value={question}
            onChange={onComposerChange}
            onInput={autoGrow}
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
        {/* While a conversation opens, sending is gated but typing is not --
            typed work must survive the load. Say so instead of looking stuck. */}
        {historyLoading && (
          <p className="composer__hint code" role="status">
            {"Opening conversation \u2014 sending is paused until it loads."}
          </p>
        )}
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
            <p className="kicker chat__empty-kicker">Ask the corpus</p>
            <h2 className="chat__empty-lead">What does the FDA guidance say?</h2>
            {/* The header subtitle already states the contract (cited answers,
                asks when unclear); this line is the instruction, not an echo. */}
            <p className="chat__empty-note">
              Ask in your own words, or start from an example below.
            </p>
            <div className="chat__starters">
              {EXAMPLE_GROUPS.map((g) => (
                <div className="starter" key={g.kind}>
                  <p className="starter__kind">{g.kind}</p>
                  <div className="chat__examples">
                    {g.items.map((ex) => (
                      <button key={ex.q} className="pill" onClick={() => sendStarter(ex.q)}>
                        {ex.label}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {historyLoading && <p className="chat__note code">Opening conversation…</p>}

        {/* Docket header: a reopened conversation states its filed identity
            (title + opened date) above the transcript, like a case caption.
            Live-only conversations have no server meta, so none renders. */}
        {sessionMeta && (
          <header className="chat__docket">
            <p className="kicker">Conversation</p>
            <h2 className="chat__docket-title">{sessionMeta.title}</h2>
            {/* Only when the wire date parses -- never "Opened" over garbage. */}
            {parseApiDate(sessionMeta.created_at) !== null && (
              <p className="chat__docket-date code">Opened {formatFiled(sessionMeta.created_at)}</p>
            )}
          </header>
        )}

        {turns.map((t, i) => {
          // Prefer a stable identity over the array index, so a turn's child
          // state (feedback, details) tracks the turn rather than its
          // position. Live assistant turns carry meta.turn_id; rehydrated
          // turns WITHOUT meta (pre-Tier-2 rows, user turns) still carry the
          // server row id, so fall through turn_id -> id -> index.
          const key = `${t.role}-${t.meta?.turn_id ?? t.id ?? i}`;
          return t.role === "user" ? (
            <UserTurn key={key} content={t.content} live={t.live} />
          ) : (
            <AssistantTurn
              key={key}
              turn={t}
              sessionId={sessionId}
              onPick={onPick}
              onCite={setActiveCitation}
              busy={busy}
              threshold={settings?.refusal_score_threshold ?? null}
            />
          );
        })}

        {/* A send that failed in transport: its inquiry turn stays above (the
            question keeps its place in the thread), and this right-aligned
            notice explains + offers Retry. Hidden while a retry is in flight
            -- the ticker takes the slot back. */}
        {!loading && failedSend && (
          <div className="chat-row chat-row--user">
            <SendFailNotice message={failedSend.message} onRetry={retryFailedSend} />
          </div>
        )}

        {/* In-flight assistant slot: the docket ticker until the first token,
            then the answer streams in as a provisional draft. The draft is
            aria-hidden — the settled answer is announced via the SR live region
            below — and both are cleared when the validated turn lands. */}
        {loading && (
          <div className="chat-row rise">
            {/* Bare avatar (no marginalia): an in-flight turn has no provenance
                yet. The margin wrapper keeps the column aligned with settled
                turns on wide viewports. */}
            <div className="chat-row__margin">
              <span className="avatar" aria-hidden>
                RW
              </span>
            </div>
            <div className="msg" aria-hidden={draft != null ? true : undefined}>
              {draft != null ? <ProvisionalDraft text={draft} /> : <StatusTicker frames={statusFrames} />}
            </div>
          </div>
        )}

        <div ref={threadEndRef} aria-hidden />
      </div>

      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>
      {/* Composer-side notices (programmatic scoping, pill filter clears,
          question recovery) get their own polite region so they never fight
          the answer-lifecycle region above for the same DOM text. */}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {composerNotice}
      </div>

      {composer}

      <EvidenceDrawer citation={activeCitation} onClose={closeDrawer} />
    </div>
  );
}
