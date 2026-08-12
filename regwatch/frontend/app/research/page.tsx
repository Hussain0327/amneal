"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAuth } from "@/components/AuthProvider";
import { CurrentProductProvider } from "@/components/CurrentProductProvider";
import { AssistantPanel } from "@/components/research/AssistantPanel";
import { HistoryPanel } from "@/components/research/HistoryPanel";
import { LegacyCenter } from "@/components/research/LegacyCenter";
import { RecordPanel } from "@/components/research/RecordPanel";
import { RecordRail } from "@/components/research/RecordRail";
import { ResearchTopBar } from "@/components/research/ResearchTopBar";
import { Sheet } from "@/components/research/Sheet";
import { WorkRail } from "@/components/research/WorkRail";
import { SessionsProvider } from "@/components/SessionsProvider";
import { SettingsProvider, useSettings } from "@/components/SettingsProvider";
import { SpineRail } from "@/components/SpineRail";
import { StatusTicker } from "@/components/StatusTicker";
import { SendIcon } from "@/components/studio/icons";
import { AssistantTurn, ProvisionalDraft, UserTurn } from "@/components/Turns";
import {
  askQueryStream,
  getSession,
  STREAM_FALLBACK_STATUS,
  type Citation,
  type Suggestion,
} from "@/lib/api";
import { citeKey } from "@/lib/citations";
import { syncTextareaHeight } from "@/lib/composer";
import {
  countSources,
  fileAuthorities,
  fileHistory,
  studioScope,
  turnAnchorId,
  turnKey,
} from "@/lib/research-record";
import {
  artifactTitle,
  authoritiesFrom,
  type ArtifactKind,
  type Authority,
  type KindGroup,
  type RecordPanelId,
  type WorkItem,
} from "@/lib/research-types";
import {
  fetchKindGroup,
  fetchWorkGroups,
  isArtifactKind,
  KIND_LABEL,
  loadingGroups,
} from "@/lib/research-work";
import { assistantTurn, nonAnswerLabel, turnFromMessage, userTurn, type Turn } from "@/lib/turns";

// THE STAGED SCOPE. Read this before you read anything below.
//
// The Research Studio is one shell over four artifact kinds. Exactly ONE of
// them -- the thread -- is built here: transcript on the sheet, composer docked
// in its footer, cited sources in the authorities margin. The other three mount
// their old surfaces through <LegacyCenter/> and become real sheets in their own
// later changes. Anything that reads as unfinished about dossiers, bulletins and
// papers on this surface is unfinished, not decided.
//
// Even the thread is a subset of 01 Ask, deliberately. Ported: the streaming
// run with its sequence guard, the live-draft channel and its fallback discard,
// clarify picks, the transcript and the evidence-free failure path. NOT yet
// ported, and each one is a real affordance the old surface still has: the
// structured ingredient/dosage filters and their scope chips, the restore chip
// for a cancelled question, the in-thread Retry for a failed send, the
// draft typewriter pacing, and the starter examples. They are missing, not
// removed: the root route still serves the full Ask surface while this is staged.

function isAbortError(e: unknown): boolean {
  return e instanceof Error && e.name === "AbortError";
}

/**
 * The authorities for what is on the sheet NOW.
 *
 * Only the newest assistant turn is ever consulted, and if that turn is a
 * refusal, a clarify, or simply uncited, the margin is empty. An earlier
 * version searched BACKWARDS for the most recent turn that happened to carry
 * citations, which meant a refusal rendered with the previous answer's sources
 * hanging beside it -- grounding attached to a claim that was never made, which
 * is the exact failure INV-2 exists to prevent. Walking back is never correct
 * here: the margin describes one turn, so it reads one turn.
 *
 * A plain function rather than a useMemo: the early returns defeated the React
 * Compiler's memoization check (it fails the lint gate), and the compiler
 * memoizes this call for free at the site.
 */
function latestAuthorities(turns: readonly Turn[]): readonly Authority[] {
  const last = turns.findLast((t) => t.role === "assistant");
  if (last === undefined || last.role !== "assistant") return [];
  if (last.status !== "answer" && last.status !== "summary") return [];
  return authoritiesFrom(last.citations);
}

function StopGlyph(): React.ReactElement {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" fill="currentColor">
      <rect x="5" y="5" width="10" height="10" rx="1.5" />
    </svg>
  );
}

export default function ResearchPage(): React.ReactElement {
  // useSearchParams -- here and inside CurrentProductProvider -- needs a
  // Suspense boundary to prerender cleanly. Same pattern as Ask and White Paper.
  return (
    <Suspense fallback={null}>
      <ResearchStudio />
    </Suspense>
  );
}

/**
 * The providers the (shell) group hands its routes, mounted here.
 *
 * They cannot live in layout.tsx: that file has to stay a server component to
 * export metadata, and SessionsProvider is keyed off the signed-in identity,
 * which only a client component can read. The key rule is copied from
 * app/(shell)/layout.tsx and is load-bearing for the same reason -- a different
 * identity remounts the session-scoped subtree, so a stale tab can never keep
 * another user's transcript in component state.
 *
 * The spine rail renders INSIDE this shell rather than being covered by the
 * studio, because it is the only way out of the room.
 */
function ResearchStudio(): React.ReactElement {
  const { user } = useAuth();
  return (
    <SessionsProvider key={user?.id ?? "anon"}>
      <SettingsProvider>
        <CurrentProductProvider>
          <div className="shell rw-shell">
            <a href="#rw-main" className="skip-link">
              Skip to content
            </a>
            <SpineRail />
            <ResearchShell />
          </div>
        </CurrentProductProvider>
      </SettingsProvider>
    </SessionsProvider>
  );
}

function ResearchShell(): React.ReactElement {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { settings } = useSettings();

  const paramKind = searchParams.get("kind");
  const paramId = searchParams.get("id");

  // WHY THE KIND IS STATE AND NOT SIMPLY THE PARAM.
  // ?kind= is where a kind ARRIVES from -- the four old routes redirect into it
  // and a thread is linkable by it -- but three of the four kinds still mount
  // their old surfaces, and White Paper writes its own ?run= onto whatever
  // pathname it finds itself at, which is now this one. Deriving the kind
  // straight from the param would let a staged child's URL write bounce the
  // studio back to threads mid-session. So the param SEEDS the state and a
  // valid param UPDATES it; an absent one is ignored rather than obeyed.
  const [kind, setKind] = useState<ArtifactKind>(() =>
    isArtifactKind(paramKind) ? paramKind : "thread",
  );
  const [activeId, setActiveId] = useState<string | null>(() => paramId);
  const [seenParams, setSeenParams] = useState<string>(`${paramKind}|${paramId}`);

  // Adjusted during render rather than in an effect: an effect would paint the
  // previous artifact first and correct it, and the first paint after a
  // deep link is the one that has to be right. Same pattern WorkRail uses to
  // open the active group.
  const paramsKey = `${paramKind}|${paramId}`;
  if (paramsKey !== seenParams) {
    setSeenParams(paramsKey);
    if (isArtifactKind(paramKind)) setKind(paramKind);
    if (paramId !== null) setActiveId(paramId);
  }

  const [groups, setGroups] = useState<readonly KindGroup[]>(loadingGroups);
  const [panel, setPanel] = useState<RecordPanelId | null>(null);
  const [workOpen, setWorkOpen] = useState(false);
  const [litN, setLitN] = useState<number | null>(null);

  // --- the thread ----------------------------------------------------------

  const [turns, setTurns] = useState<Turn[]>([]);
  const [threadTitle, setThreadTitle] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  // Transport-register failures only: a send that never reached the server, or
  // a thread that would not open. Never an epistemic verdict -- a refusal is
  // the turn's own business and renders in the transcript (INV-2).
  const [notice, setNotice] = useState<string | null>(null);
  const [statusFrames, setStatusFrames] = useState<string[]>([]);
  const [draft, setDraft] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  // Mirrors sessionIdRef for render: a ref read during render is not a value
  // React can track, and the settled turns need the session identity.
  const [sessionId, setSessionId] = useState<string | null>(null);

  const sessionIdRef = useRef<string | null>(null);
  // One run at a time. A stale run discovers it lost through the sequence, so a
  // late frame from an abandoned stream can never write into the live thread.
  const runSeqRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const lastQuestionRef = useRef("");
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  const busy = loading || historyLoading;
  const threadId = kind === "thread" ? activeId : null;

  // --- the work list -------------------------------------------------------

  // Every in-flight work fetch, so an unmount mid-load leaves nothing writing
  // into a dead component. The fetches themselves cannot be cancelled (the api
  // layer threads no signal through GET), so what the signal buys is a
  // guaranteed-ignored result, which is the honest half of the guarantee.
  const workControllers = useRef(new Set<AbortController>());

  useEffect(() => {
    const live = workControllers.current;
    return () => {
      live.forEach((c) => c.abort());
      live.clear();
    };
  }, []);

  const trackWork = useCallback((): AbortController => {
    const controller = new AbortController();
    workControllers.current.add(controller);
    return controller;
  }, []);

  const reloadWork = useCallback(() => {
    const controller = trackWork();
    // Deliberately does NOT reset the rail to "loading": a reload after a send
    // would otherwise blank four lists the analyst is looking at to refresh one
    // row. The first paint is already loading (see the initial state).
    void fetchWorkGroups(controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setGroups(next);
      })
      .finally(() => workControllers.current.delete(controller));
  }, [trackWork]);

  const retryKind = useCallback(
    (target: ArtifactKind) => {
      const controller = trackWork();
      setGroups((prev) =>
        prev.map(
          (g): KindGroup =>
            g.kind === target ? { ...g, state: "loading", items: [], total: 0 } : g,
        ),
      );
      void fetchKindGroup(target, controller.signal)
        .then((group) => {
          if (controller.signal.aborted) return;
          setGroups((prev) => prev.map((g): KindGroup => (g.kind === target ? group : g)));
        })
        .finally(() => workControllers.current.delete(controller));
    },
    [trackWork],
  );

  useEffect(() => {
    // reloadWork sets state only after the awaited fan-out resolves, never
    // synchronously in this effect body, so the set-state-in-effect rule does
    // not fire here.
    reloadWork();
  }, [reloadWork]);

  // --- opening an artifact -------------------------------------------------

  const writeUrl = useCallback(
    (nextKind: ArtifactKind, nextId: string | null) => {
      // The LIVE query string, not the render-time snapshot: a staged surface
      // may have written its own params since this render (White Paper writes
      // ?run=), and a snapshot would silently roll them back.
      const params = new URLSearchParams(window.location.search);
      params.set("kind", nextKind);
      if (nextId === null) params.delete("id");
      else params.set("id", nextId);
      // STAGING. The White Paper view opens a run from its OWN ?run= param, so
      // a paper picked in the rail has to be written in that view's vocabulary
      // as well as in the studio's, or selecting one would switch the kind and
      // show somebody else's run. Dossiers and bulletins have no equivalent
      // param and no equivalent hook: picking one switches the kind and nothing
      // else, which is the staging showing through. It goes when they get
      // sheets.
      if (nextKind === "paper" && nextId !== null) params.set("run", nextId);
      else params.delete("run");
      router.replace(`/research?${params.toString()}`, { scroll: false });
    },
    [router],
  );

  const openArtifact = useCallback(
    (nextKind: ArtifactKind, nextId: string | null) => {
      // A switch always cancels the stream the previous artifact owned: its
      // answer belongs to a thread that is no longer on the sheet. Bumping the
      // sequence orphans the run, so its finally block will not clear these --
      // hence the explicit resets.
      controllerRef.current?.abort();
      controllerRef.current = null;
      runSeqRef.current += 1;
      setLoading(false);
      setStatusFrames([]);
      setDraft(null);
      setKind(nextKind);
      setActiveId(nextId);
      setLitN(null);
      setNotice(null);
      setWorkOpen(false);
      writeUrl(nextKind, nextId);
    },
    [writeUrl],
  );

  const onSelect = useCallback(
    (item: WorkItem) => openArtifact(item.kind, item.id),
    [openArtifact],
  );

  const onMake = useCallback(
    (nextKind: ArtifactKind) => openArtifact(nextKind, null),
    [openArtifact],
  );

  // --- the transcript ------------------------------------------------------

  // Loading a thread resets the whole centre and then fetches. Every setState
  // here is an intentional reset or the awaited result, not a cascading render;
  // set-state-in-effect is disabled for the block the same way the Ask page
  // disables it for its session sync.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (kind !== "thread") return;
    // The thread this page just created live. Its transcript is already on the
    // sheet, freshly streamed, and refetching would throw away the live turn's
    // reveal and its status log to redraw the same words from the server. It is
    // also how a kind switch and back returns to the thread it left rather than
    // reloading it. Same rule the Ask page applies to its own URL sync.
    if (threadId !== null && threadId === sessionIdRef.current) return;
    sessionIdRef.current = threadId;
    setSessionId(threadId);
    setTurns([]);
    setThreadTitle(null);
    setNotice(null);
    setLitN(null);
    if (threadId === null) return; // a new thread opens empty, with no fetch
    let cancelled = false;
    setHistoryLoading(true);
    getSession(threadId)
      .then((d) => {
        if (cancelled) return;
        setThreadTitle(d.session.title);
        setTurns(d.messages.map(turnFromMessage));
      })
      .catch((e) => {
        if (cancelled) return;
        // The thread would not open. Drop the identity so the next send starts
        // a fresh session rather than silently appending to a conversation
        // nobody can see, and say what happened over an empty sheet.
        sessionIdRef.current = null;
        setSessionId(null);
        setNotice(
          `Could not open this thread - ${e instanceof Error ? e.message : String(e)}. Start a new thread, or reload the page.`,
        );
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, threadId]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // Abort an in-flight stream on unmount so nothing writes into a dead page.
  useEffect(
    () => () => {
      controllerRef.current?.abort();
    },
    [],
  );

  const run = useCallback(
    async (q: string, filters: Record<string, string> | null) => {
      if (busy) return;
      const seq = ++runSeqRef.current;
      const controller = new AbortController();
      controllerRef.current = controller;
      setLoading(true);
      setNotice(null);
      setStatusFrames([]);
      setDraft(null);
      setAnnouncement("");
      setLitN(null);
      lastQuestionRef.current = q;
      // The question joins the thread immediately; the ticker answers it in
      // place. It is client-only until the result lands.
      setTurns((prev) => [...prev, userTurn(q)]);
      setQuestion("");

      // Accumulated in the closure, not in state: statusFrames is wiped in
      // finally before the settled turn renders, so this is the only record
      // that can be persisted onto the turn as provenance.
      const frames: string[] = [];
      let fellBack = false;
      // The post-audit token replay carries the SAME answer the live draft has
      // already painted. Without this the reply types itself twice.
      let sawDraft = false;

      try {
        const next = await askQueryStream(
          q,
          filters,
          sessionIdRef.current,
          {
            onStatus: (text) => {
              if (runSeqRef.current !== seq) return;
              frames.push(text);
              if (text === STREAM_FALLBACK_STATUS) {
                // The dead stream's provisional tokens are contractually
                // discarded, so the draft goes with them and the ticker takes
                // the slot back for the re-run.
                fellBack = true;
                setDraft(null);
              }
              setStatusFrames((prev) => [...prev, text]);
            },
            onToken: (delta) => {
              if (runSeqRef.current !== seq || sawDraft) return;
              setDraft((prev) => (prev ?? "") + delta);
            },
            onDraft: (delta) => {
              if (runSeqRef.current !== seq) return;
              sawDraft = true;
              setDraft((prev) => (prev ?? "") + delta);
            },
            onDraftReset: () => {
              if (runSeqRef.current !== seq) return;
              setDraft(null);
            },
          },
          true,
          controller.signal,
        );
        if (runSeqRef.current !== seq || controller.signal.aborted) return;
        sessionIdRef.current = next.session_id;
        setSessionId(next.session_id);
        setDraft(null);
        setTurns((prev) => [
          ...prev,
          assistantTurn(next, {
            statusLog: frames,
            streamFellBack: fellBack,
            draftWithdrawn: next.draft_withdrawn ?? null,
          }),
        ]);
        const declined = nonAnswerLabel(next.status, next.refused, next.reason ?? null);
        setAnnouncement(
          declined
            ? `${declined} - see the reply.`
            : next.status === "clarify"
              ? "Clarification requested - see the reply."
              : "Answer ready.",
        );
        if (activeId !== next.session_id) {
          setActiveId(next.session_id);
          writeUrl("thread", next.session_id);
        }
        // The thread is either new or has just moved to the top of the rail.
        reloadWork();
      } catch (e) {
        if (isAbortError(e) || runSeqRef.current !== seq || controller.signal.aborted) return;
        // The send never reached the server. Pop the optimistic turn -- leaving
        // it would show an unanswered question that looks filed -- and hand the
        // words back to an empty composer so nothing typed is lost.
        setTurns((prev) =>
          prev.length > 0 && prev[prev.length - 1].role === "user" ? prev.slice(0, -1) : prev,
        );
        setQuestion((cur) => (cur.trim() ? cur : q));
        setNotice(`Not sent - ${e instanceof Error ? e.message : String(e)}. Send it again.`);
      } finally {
        if (runSeqRef.current === seq) {
          setLoading(false);
          setStatusFrames([]);
          setDraft(null);
          controllerRef.current = null;
        }
      }
    },
    [busy, activeId, writeUrl, reloadWork],
  );

  // Stable across the per-token re-renders of a streaming answer, so the memo
  // on the settled turn components actually bails out and no settled answer
  // re-parses its markdown once per frame.
  const onPick = useCallback(
    (opt: Suggestion) => {
      if (busy) return;
      // Clarify filters are dict[str, Any] on the wire and deterministic string
      // maps in practice; narrowed here exactly as the Ask page narrows them.
      void run(opt.query, (opt.filters ?? null) as Record<string, string> | null);
    },
    [busy, run],
  );

  // --- the authorities margin ----------------------------------------------

  // The margin holds the LATEST cited answer's sources, not every source the
  // thread ever produced: it is the margin of what is on the sheet now, and a
  // thread's tenth answer does not carry the first one's authorities.
  //
  // Gated on status as well as on the array, defending INV-2 a second time: a
  // refused or clarify turn renders no citation surface in the transcript, and
  // the margin must agree rather than resurrect grounding beside a refusal.
  const authorities = latestAuthorities(turns);

  // Clicking a [n] stamp lights its authority. The margin is the evidence view
  // for what is ON THE SHEET, which is the whole thesis: in the Compliance
  // Studio marks go ON the text because they are the analyst's own hand; here
  // they go beside it because they are the record's. The record drawer behind
  // the rail is a different question -- everything the artifact has ever stood
  // on, rather than what this turn cites -- and the two are numbered by the
  // same call so they cannot disagree about which source is [3].
  const onCite = useCallback(
    (c: Citation) => {
      const key = citeKey(c.short_name, c.page);
      const hit = authorities.find((a) => citeKey(a.shortName, a.page) === key);
      // A stamp on an OLDER turn has no row in the current margin, so nothing
      // lights. Better than lighting the wrong row.
      setLitN(hit ? hit.n : null);
    },
    [authorities],
  );

  // --- the record drawer ---------------------------------------------------

  // Three views of one thing, all derived from the transcript and none of them
  // fetched: what the artifact stands on, what was asked of it, and what
  // product its own sources say it is about. Plain calls rather than useMemo --
  // the React Compiler memoizes them at the site, and a hand-rolled memo here
  // would be a second cache to keep honest.
  const filings = fileAuthorities(turns);
  const trail = fileHistory(turns);
  const scope = studioScope(turns);

  const closePanel = useCallback(() => setPanel(null), []);

  /**
   * Scroll the transcript to the turn a drawer row describes.
   *
   * Nothing to fall back to when the anchor is missing -- a rehydrated turn the
   * transcript has since replaced -- so the click does nothing rather than
   * scrolling somewhere arbitrary and claiming to have arrived.
   *
   * The reduced-motion check is read at click time, not cached: the setting can
   * change while the studio is open, and a long transcript smooth-scrolled past
   * twenty turns is exactly the motion the preference exists to stop.
   */
  const onJump = useCallback((key: string) => {
    const target = document.getElementById(turnAnchorId(key));
    if (target === null) return;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
  }, []);

  // --- the composer --------------------------------------------------------

  // Programmatic value changes (cleared after a send, refilled on stop) do not
  // fire onInput, so the height would otherwise stick at the old content.
  useEffect(() => {
    syncTextareaHeight(composerRef.current);
  }, [question]);

  function submit(): void {
    const q = question.trim();
    if (!q || busy) return;
    void run(q, null);
  }

  function stop(): void {
    if (!loading) return;
    controllerRef.current?.abort();
    setDraft(null);
    setTurns((prev) =>
      prev.length > 0 && prev[prev.length - 1].role === "user" ? prev.slice(0, -1) : prev,
    );
    // Hand the cancelled question back, but never over text the analyst has
    // started typing since: their words win the composer.
    setQuestion((cur) => (cur.trim() ? cur : lastQuestionRef.current));
    composerRef.current?.focus();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>): void {
    // Enter sends, Shift+Enter is a newline. Skipped while an IME is composing,
    // where Enter commits the candidate rather than the message.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  const composer = (
    <form
      className="rw-composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      {notice && (
        <p className="rw-composer__note rw-composer__note--fail" role="alert">
          {notice}
        </p>
      )}
      <div className="rw-composer__bar">
        <textarea
          ref={composerRef}
          className="rw-composer__input"
          rows={1}
          value={question}
          placeholder={
            historyLoading
              ? "Opening thread..."
              : turns.length > 0
                ? "Ask a follow-up, or start a new question"
                : "Ask about an FDA guidance, product, or change"
          }
          aria-label="Ask the guidance corpus"
          onChange={(e) => setQuestion(e.target.value)}
          onInput={(e) => syncTextareaHeight(e.currentTarget)}
          onKeyDown={onKeyDown}
        />
        {loading ? (
          <button
            type="button"
            className="rw-composer__send"
            aria-label="Stop generating"
            onClick={stop}
          >
            <StopGlyph />
          </button>
        ) : (
          <button
            type="submit"
            className="rw-composer__send"
            aria-label="Send"
            disabled={busy || !question.trim()}
          >
            <SendIcon />
          </button>
        )}
      </div>
      {/* Typing is never blocked while a thread opens -- typed work must survive
          the load -- so say why sending is, instead of looking stuck. */}
      {historyLoading && (
        <p className="rw-composer__note" role="status">
          Opening thread - sending is paused until it loads.
        </p>
      )}
    </form>
  );

  const transcript = (
    <div className="rw-thread">
      {turns.length === 0 && !loading && !historyLoading && (
        <p className="rw-thread__empty">
          Ask about an FDA guidance, a product, or a change. Every claim comes back cited, and the
          sources it used hang in the margin.
        </p>
      )}

      {turns.map((t, i) => {
        // A stable identity beats the index so a turn's child state tracks the
        // turn rather than its position: live turns carry meta.turn_id,
        // rehydrated ones the server row id, and only then the index. Read from
        // lib/research-record rather than built here, because the record drawer
        // points at these turns by the same string and two copies of it would
        // drift by a hyphen and silently stop scrolling anywhere.
        const key = turnKey(t, i);
        return (
          // The anchor the drawer scrolls to. A wrapper rather than an id on the
          // turn itself: UserTurn and AssistantTurn are shared with the Ask
          // surface, and this studio's navigation is no reason to widen their
          // props. The wrapper is a plain block, so the column's flex gap and
          // every .chat-row rule land exactly as before.
          <div id={turnAnchorId(key)} key={key}>
            {t.role === "user" ? (
              <UserTurn content={t.content} live={t.live} />
            ) : (
              <AssistantTurn
                turn={t}
                sessionId={sessionId}
                onPick={onPick}
                onCite={onCite}
                busy={busy}
                threshold={settings?.refusal_score_threshold ?? null}
              />
            )}
          </div>
        );
      })}

      {/* In-flight slot: the docket ticker until the first token, then the
          answer as a provisional draft. The draft is aria-hidden -- the settled
          answer is announced through the live region on the shell. */}
      {loading && (
        <div className="chat-row">
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
    </div>
  );

  // --- the shell -----------------------------------------------------------

  // Scoped to the kind on the sheet, not searched across all four. The param
  // sync above sets kind and id independently, so a stale or hand-edited
  // /research?kind=thread&id=<a-paper-id> arrives with the two disagreeing --
  // and an unscoped lookup would then title a Thread sheet with a paper's name.
  // No match is the honest answer there, and artifactTitle says "Untitled".
  const activeItem = useMemo(
    () => groups.find((g) => g.kind === kind)?.items.find((item) => item.id === activeId) ?? null,
    [groups, kind, activeId],
  );

  // "New thread" is only true with no id in hand; see artifactTitle. The rail
  // may still be loading or unreachable, and neither is a reason to tell the
  // analyst the artifact they just opened is a blank they made.
  const title = artifactTitle(kind, activeId, threadTitle, activeItem);

  // The work rail takes no `open` prop, so its narrow-viewport slide-over state
  // is the shell's: css/work-rail.css answers .is-work-open on this root.
  return (
    <div className={`rw-studio${workOpen ? " is-work-open" : ""}`}>
      <ResearchTopBar
        crumb={{ kindLabel: KIND_LABEL[kind], title }}
        onToggleWork={() => setWorkOpen((v) => !v)}
      />

      {/* One live region for the whole studio, mounted unconditionally: a region
          that appears at the same moment as its first message is not announced. */}
      <div className="rw-sr" role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>

      <div className="rw-body">
        <WorkRail
          groups={groups}
          activeId={activeId}
          onSelect={onSelect}
          onMake={onMake}
          onRetry={retryKind}
        />

        <div className="rw-desk" id="rw-main" tabIndex={-1}>
          {kind === "thread" ? (
            <Sheet
              kicker="Thread"
              title={title}
              authorities={authorities}
              // A verdict needs a settled turn. Nothing has landed on a new
              // thread, and a streaming or opening one has not finished.
              settled={!busy && turns.some((t) => t.role === "assistant")}
              litN={litN}
              onLit={setLitN}
              footer={composer}
            >
              {transcript}
            </Sheet>
          ) : (
            <LegacyCenter kind={kind} />
          )}
        </div>

        {/* The record drawer. Three cards over one artifact: what it is made
            of, a way to ask about it, and how it came to say what it says.
            Closed by default -- the artifact is the work and the record is the
            lookup -- and only one is ever mounted, because only one is ever
            open and a hidden card holding an in-flight question is a request
            nobody can see. */}
        {panel === "record" && (
          <RecordPanel kind={kind} filings={filings} onJump={onJump} onClose={closePanel} />
        )}
        {panel === "assistant" && (
          <AssistantPanel
            kindLabel={KIND_LABEL[kind]}
            title={title}
            scope={scope}
            sourceCount={countSources(filings)}
            questionCount={filings.length}
            onClose={closePanel}
          />
        )}
        {panel === "history" && (
          <HistoryPanel kind={kind} entries={trail} onJump={onJump} onClose={closePanel} />
        )}

        <RecordRail
          panel={panel}
          onTogglePanel={(id) => setPanel((prev) => (prev === id ? null : id))}
        />

        {workOpen && (
          <button
            type="button"
            className="rw-scrim"
            aria-label="Close your work"
            onClick={() => setWorkOpen(false)}
          />
        )}
      </div>
    </div>
  );
}
