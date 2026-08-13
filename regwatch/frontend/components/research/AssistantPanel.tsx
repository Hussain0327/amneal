"use client";

// ASSISTANT: the studio's own reading of what is in front of you, and a way to
// ask about it.
//
// WHAT IT CAN SEE, STATED RATHER THAN IMPLIED
// The head of this panel is a manifest: the artifact on the sheet, the product
// its own sources identify, and how much record it is standing on. That list is
// the whole contract. An assistant that gestures at "your screen" and then
// answers from something narrower is the failure mode here, so the panel names
// each thing it is holding and lets the analyst take any of it away. The scope
// chip is a control, not a caption.
//
// WHY IT IS NOT THE COMPOSER AGAIN
// The composer under the sheet asks the corpus and its answer joins the record.
// This asks the same corpus already narrowed to what the artifact is about, and
// keeps its answers out of the record: a question you ask to understand the
// artifact is not a claim the artifact makes. Both are cited, because
// everything here is.
//
// WHERE ITS CONVERSATION LIVES, SAID OUT LOUD
// /query has one place to put a conversation, so this one is a session like
// any other -- it persists, and it is readable and deletable by id exactly
// like a thread. The foot of the panel says so, in short. What it does not
// do is show up in the work rail's Threads list: this panel writes its
// session with origin "assistant", and ListChatSessionsForUser filters that
// origin out. A lookup you make to understand an artifact is not the
// analyst's own work, so it does not belong on a list built to show that
// work. It is still one persistent session, not either alternative: a fresh
// one per question would leave an unreachable orphan behind every question
// anybody asked, and reusing the artifact's own session would grow the
// audit record of a filing with questions nobody meant to file.

import { useCallback, useEffect, useRef, useState } from "react";

import { PanelFrame } from "@/components/research/PanelFrame";
import { StatusTicker } from "@/components/StatusTicker";
import { CloseIcon, SendIcon } from "@/components/studio/icons";
import { askQueryStream, type Citation } from "@/lib/api";
import { syncTextareaHeight } from "@/lib/composer";
import { dedupeCitations } from "@/lib/citations";
import type { StudioScope } from "@/lib/research-record";
import { nonAnswerLabel } from "@/lib/turns";

/** One line of the panel's own conversation. */
interface AssistantEntry {
  readonly role: "you" | "rw" | "context";
  readonly text: string;
  /**
   * Whether this turn was one that COULD carry grounding.
   *
   * The whole INV-2 boundary for this panel, and it has to be its own field
   * rather than inferred from an absent decline label. Three of the wire's
   * statuses are neither answers nor declines: `clarify` asks a question back,
   * `meta` is conversational, and both return null from nonAnswerLabel. Reading
   * "no decline label" as "this answered" put the sentence "Not drawn from the
   * record" under a clarifying question -- stating that grounding was owed and
   * missing when none was ever owed. That is the same boundary drawn in the
   * same wrong place that HistoryPanel's SourceLine exists to avoid.
   */
  readonly answered?: boolean;
  /** Only ever set on an "rw" line, and only once it has settled. */
  readonly sources?: readonly Citation[];
  /** The decline register, when the turn did not answer. */
  readonly declined?: string;
}

interface AssistantPanelProps {
  /** "Threads" / "Dossiers" ... as the top bar names the kind. */
  readonly kindLabel: string;
  readonly title: string;
  /** The product the artifact's own sources identify, or null. */
  readonly scope: StudioScope | null;
  /** Distinct sources across the artifact, and the questions that fetched them. */
  readonly sourceCount: number;
  readonly questionCount: number;
  readonly onClose: () => void;
}

/** The manifest line for the scope, with its own off switch. */
function ScopeRow({
  scope,
  on,
  onToggle,
}: {
  readonly scope: StudioScope | null;
  readonly on: boolean;
  readonly onToggle: () => void;
}): React.JSX.Element {
  if (scope === null) {
    return (
      <p className="rw-see__row">
        <span className="rw-see__tag">Product</span>
        <span className="rw-see__val rw-see__val--none">
          Not identified yet — nothing here has cited a guidance
        </span>
      </p>
    );
  }
  const label = scope.dosageForm === null
    ? scope.normalizedName
    : `${scope.normalizedName} · ${scope.dosageForm}`;
  return (
    <p className="rw-see__row">
      <span className="rw-see__tag">Product</span>
      {on ? (
        <span className="rw-see__val">
          <span className="rw-scope">
            {label}
            <button
              type="button"
              className="rw-scope__off"
              onClick={onToggle}
              aria-label={`Stop narrowing to ${label}`}
            >
              <CloseIcon size={10} />
            </button>
          </span>
        </span>
      ) : (
        <span className="rw-see__val">
          <button type="button" className="rw-see__restore" onClick={onToggle}>
            Narrow to {label}
          </button>
        </span>
      )}
    </p>
  );
}

export function AssistantPanel({
  kindLabel,
  title,
  scope,
  sourceCount,
  questionCount,
  onClose,
}: AssistantPanelProps): React.JSX.Element {
  const [entries, setEntries] = useState<readonly AssistantEntry[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [statusFrames, setStatusFrames] = useState<string[]>([]);
  const [reply, setReply] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [scopeOn, setScopeOn] = useState(true);
  const [announcement, setAnnouncement] = useState("");

  // WHERE THE ARTIFACT CHANGE IS NOTICED. Adjusted during render rather than in
  // an effect, the same pattern the shell uses for its URL params: an effect
  // would paint the old artifact's manifest first and correct it, and the
  // manifest's whole job is to be true at the moment it is read.
  const artifact = `${kindLabel}|${title}`;
  const [seenArtifact, setSeenArtifact] = useState(artifact);
  if (artifact !== seenArtifact) {
    setSeenArtifact(artifact);
    // Only worth filing once there is a conversation for it to interrupt. The
    // line is the honest record of a fact that changed mid-conversation: what
    // was asked before it was asked about something else.
    setEntries((prev) =>
      prev.length === 0 ? prev : [...prev, { role: "context", text: `Now looking at ${title}` }],
    );
  }

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  /** The panel's conversation. Null until the first answer names one. */
  const sessionRef = useRef<string | null>(null);
  /** One run at a time; a stale run discovers it lost through the sequence. */
  const seqRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);

  // Follow the answer down. Each token is a render, so pinning here keeps the
  // newest line in view without a scroll listener.
  useEffect(() => {
    const el = scrollRef.current;
    if (el !== null) el.scrollTop = el.scrollHeight;
  }, [entries, reply, statusFrames]);

  // Nothing writes into a dead panel.
  useEffect(
    () => () => {
      controllerRef.current?.abort();
    },
    [],
  );

  const send = useCallback(async (): Promise<void> => {
    const q = question.trim();
    if (q === "" || busy) return;
    const seq = ++seqRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setNotice(null);
    setStatusFrames([]);
    setReply(null);
    setEntries((prev) => [...prev, { role: "you", text: q }]);
    setQuestion("");

    // Only the filters the analyst has left switched on, and dosage_form rides
    // along only with a product -- a bare form is not a scope, it is a guess.
    // Built as a plain map rather than an object literal with an optional key,
    // because the wire type is Record<string, string> and an absent filter must
    // be an absent KEY, not a key holding undefined.
    let filters: Record<string, string> | null = null;
    if (scopeOn && scope !== null) {
      filters = { normalized_name: scope.normalizedName };
      if (scope.dosageForm !== null) filters["dosage_form"] = scope.dosageForm;
    }

    try {
      const next = await askQueryStream(
        q,
        filters,
        sessionRef.current,
        {
          onStatus: (text) => {
            if (seqRef.current !== seq) return;
            setStatusFrames((prev) => [...prev, text]);
          },
          onToken: (delta) => {
            if (seqRef.current !== seq) return;
            setReply((prev) => (prev ?? "") + delta);
          },
        },
        // No provisional-draft channel here. The sheet earns the live draft
        // because an analyst waits on it; a lookup does not, and a draft that
        // can be withdrawn needs the whole withdrawal surface to go with it.
        false,
        controller.signal,
        // See the file-head comment: this session is real, but it is not the
        // analyst's own work, so it must not land in the Threads list.
        "assistant",
      );
      if (seqRef.current !== seq || controller.signal.aborted) return;
      // The conversation's identity, adopted from the first answer and reused
      // for the rest -- otherwise every question would file its own thread.
      sessionRef.current = next.session_id;
      // INV-2: sources ride only with a turn that actually answered, and the
      // "not sourced" verdict is only ever spoken about one of those.
      const answered = next.status === "answer" || next.status === "summary";
      const declined = nonAnswerLabel(next.status, next.refused, next.reason ?? null);
      setEntries((prev) => [
        ...prev,
        {
          role: "rw",
          text: next.answer,
          answered,
          sources: answered ? next.citations : [],
          declined: declined ?? undefined,
        },
      ]);
      // The reply lands in plain state, which a screen reader has no reason to
      // revisit, so the outcome is spoken here. Same three sentences the shell
      // announces for the sheet, so the two conversations sound alike.
      setAnnouncement(
        declined !== null
          ? `${declined} - see the reply.`
          : next.status === "clarify"
            ? "Clarification requested - see the reply."
            : "Answer ready.",
      );
    } catch (e) {
      if (seqRef.current !== seq || controller.signal.aborted) return;
      if (e instanceof Error && e.name === "AbortError") return;
      // Pop the question rather than leave it sitting unanswered, and hand the
      // words back so nothing typed is lost. Same contract as the composer.
      setEntries((prev) =>
        prev.length > 0 && prev[prev.length - 1].role === "you" ? prev.slice(0, -1) : prev,
      );
      setQuestion((cur) => (cur.trim() !== "" ? cur : q));
      setNotice(`Not sent — ${e instanceof Error ? e.message : String(e)}. Send it again.`);
    } finally {
      if (seqRef.current === seq) {
        setBusy(false);
        setStatusFrames([]);
        setReply(null);
        controllerRef.current = null;
      }
    }
  }, [question, busy, scope, scopeOn]);

  const onSubmit = useCallback(
    (e: React.FormEvent): void => {
      e.preventDefault();
      void send();
    },
    [send],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
      // Enter mid-composition commits an IME candidate; it is not a send.
      if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
      e.preventDefault();
      void send();
    },
    [send],
  );

  return (
    <PanelFrame
      label="Assistant"
      onClose={onClose}
      context={
        // THE MANIFEST. Everything this assistant is holding, named. It is a
        // list rather than a sentence because a list can be checked.
        <div className="rw-see">
          <p className="rw-see__row">
            <span className="rw-see__tag">Open</span>
            <span className="rw-see__val">
              {kindLabel} · {title}
            </span>
          </p>
          <ScopeRow scope={scope} on={scopeOn} onToggle={() => setScopeOn((v) => !v)} />
          <p className="rw-see__row">
            <span className="rw-see__tag">Record</span>
            <span className={`rw-see__val${sourceCount === 0 ? " rw-see__val--none" : ""}`}>
              {sourceCount === 0
                ? "No sources filed yet"
                : `${sourceCount} ${sourceCount === 1 ? "source" : "sources"} across ${questionCount} ${
                    questionCount === 1 ? "question" : "questions"
                  }`}
            </span>
          </p>
        </div>
      }
      foot={
        <form className="rw-ask" onSubmit={onSubmit}>
          <div className="rw-composer__bar">
            <textarea
              ref={boxRef}
              rows={1}
              className="rw-composer__input"
              value={question}
              placeholder={
                scopeOn && scope !== null
                  ? `Ask about ${scope.normalizedName}`
                  : "Ask about this artifact"
              }
              aria-label="Ask the assistant about this artifact"
              onChange={(e) => setQuestion(e.target.value)}
              onInput={(e) => syncTextareaHeight(e.currentTarget)}
              onKeyDown={onKeyDown}
            />
            <button
              type="submit"
              className="rw-composer__send"
              disabled={busy || question.trim() === ""}
              aria-label="Send"
            >
              <SendIcon />
            </button>
          </div>
          {notice !== null && (
            <p className="rw-composer__note rw-composer__note--fail" role="status">
              {notice}
            </p>
          )}
          <p className="rw-composer__note">
            Cited like everything here. Kept on its own, not in Threads or in this artifact&apos;s
            record.
          </p>
        </form>
      }
    >
      {/* Mounted unconditionally and never conditionally rendered: a live
          region that appears at the same moment as its first message is not
          announced. Exactly the rule the shell's own region follows. */}
      <div className="rw-sr" role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>

      <div className="rw-chat" ref={scrollRef}>
        {entries.length === 0 && reply === null && !busy && (
          <p className="rw-chat__open">
            Ask about what is on the sheet — what a guidance requires, what a source actually says,
            or where a claim came from. Answers are drawn from the record and cited.
          </p>
        )}

        {/* Keyed by index, which is safe HERE and nowhere near a general rule:
            this list is append-only, never reordered and never spliced, so an
            index is a stable identity for the lifetime of the panel. */}
        {entries.map((entry, i) =>
          entry.role === "context" ? (
            <p className="rw-chat__ctx" key={i}>
              {entry.text}
            </p>
          ) : (
            <div className={`rw-chat__turn rw-chat__turn--${entry.role}`} key={i}>
              <span className="rw-chat__who" aria-hidden="true">
                {entry.role === "you" ? "You" : "RW"}
              </span>
              <div className="rw-chat__body">
                {entry.declined !== undefined && (
                  <span className="rw-out rw-out--declined">{entry.declined}</span>
                )}
                <p className="rw-chat__text">{entry.text}</p>
                {/* Gated on `answered`, not on the absence of a decline label.
                    A clarify and a conversational turn carry neither sources
                    nor a decline, and saying anything about their grounding
                    would be a verdict on a claim that was never made. */}
                {entry.answered === true &&
                  entry.sources !== undefined &&
                  (entry.sources.length > 0 ? (
                    <ul className="rw-chat__src">
                      {dedupeCitations([...entry.sources]).map((c) => (
                        <li className="rw-chat__cite" key={`${c.short_name}-${c.page}`}>
                          {c.short_name} <span className="rw-chat__pg">p.{c.page}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    // Stated, never left blank: an ANSWER with no sources shown
                    // and no sentence saying so reads as one whose sources are
                    // merely out of sight. Only an answer ever reaches here.
                    <p className="rw-chat__nosrc">Not drawn from the record.</p>
                  ))}
              </div>
            </div>
          ),
        )}

        {busy && (
          <div className="rw-chat__turn rw-chat__turn--rw">
            <span className="rw-chat__who" aria-hidden="true">
              RW
            </span>
            <div className="rw-chat__body">
              {reply === null ? (
                <StatusTicker frames={statusFrames} />
              ) : (
                <p className="rw-chat__text">{reply}</p>
              )}
            </div>
          </div>
        )}
      </div>
    </PanelFrame>
  );
}
