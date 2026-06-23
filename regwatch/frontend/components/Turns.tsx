"use client";

import { AnswerFeedback } from "@/components/AnswerFeedback";
import { Markdown } from "@/components/Markdown";
import { RecencyBadge } from "@/components/RecencyBadge";
import type { Citation, Suggestion } from "@/lib/api";
import { confidenceBand, reasonCopy, type Turn } from "@/lib/turns";
import { safeHref } from "@/lib/url";

// Ask renders as a cited chat: the user's line as a bubble, the assistant as a
// navy avatar + message (gold is reserved to grounding). Epistemic status still
// governs the register — a cited
// finding carries citation chips with inspectable sources; a refusal/out-of-
// scope reply keeps the oxblood "declined" treatment and is never dressed as an
// answer (INV-2); clarify offers pickable options instead of guessing. The
// grounding evidence (INV-1) and the audit trail are always one tap away.

function FileGlyph() {
  return (
    <svg className="cite__icon" viewBox="0 0 16 16" aria-hidden="true" fill="none">
      <path d="M4 1.6h5L12.5 5.3v9a.6.6 0 0 1-.6.6h-8a.6.6 0 0 1-.6-.6v-12a.6.6 0 0 1 .6-.6Z" stroke="currentColor" strokeWidth="1.1" />
      <path d="M9 1.6V5.3h3.5" stroke="currentColor" strokeWidth="1.1" />
    </svg>
  );
}

// `live` gates the .rise reveal: turns that arrived this session animate in;
// turns rehydrated from history render static so a reopened conversation opens
// like a document (matches the no-auto-scroll-on-rehydrate intent in page.tsx).
export function UserTurn({ content, live }: { content: string; live: boolean }) {
  return (
    <div className={`chat-row chat-row--user${live ? " rise" : ""}`}>
      <div className="bubble bubble--user">{content}</div>
    </div>
  );
}

function AuditLine({ turn }: { turn: Turn }) {
  if (!turn.meta) return null;
  // model_name is absent on rehydrated history turns — drop the trailing
  // separator rather than render "audit #N · ".
  return (
    <p className="msg__audit code">
      audit #{turn.meta.audit_id}
      {turn.meta.model_name ? ` · ${turn.meta.model_name}` : ""}
    </p>
  );
}

// One citation, compact: opens the in-app evidence drawer instead of bouncing the
// analyst out to the remote FDA PDF. The PDF link still lives inside the drawer and
// in the <details> Sources list below (the no-JS fallback).
function CiteChip({ c, onSelect }: { c: Citation; onSelect: (c: Citation) => void }) {
  return (
    <button type="button" className="cite" onClick={() => onSelect(c)} title={`${c.short_name} · p.${c.page}`}>
      <FileGlyph />
      <span className="cite__label">
        {c.short_name} · p.{c.page}
      </span>
    </button>
  );
}

// The assistant frame: navy RW avatar + a message column the status branches
// fill. (Gold is reserved to grounding — see globals.css.) `live` gates .rise.
function AssistantShell({ children, live }: { children: React.ReactNode; live: boolean }) {
  return (
    <div className={`chat-row${live ? " rise" : ""}`}>
      <span className="avatar" aria-hidden>
        RW
      </span>
      <div className="msg">{children}</div>
    </div>
  );
}

export function AssistantTurn({
  turn,
  sessionId,
  onPick,
  onCite,
  busy,
}: {
  turn: Turn;
  sessionId: string | null;
  onPick: (s: Suggestion) => void;
  onCite: (c: Citation) => void;
  busy: boolean;
}) {
  if (turn.status === "clarify") {
    const why = reasonCopy(turn.reason);
    return (
      <AssistantShell live={turn.live}>
        <div className="msg__body">{turn.interpretation || turn.content}</div>
        {/* Why we asked instead of answered — plain-language, persisted across
            history. Text only, so INV-2 holds (no citation surface). */}
        {why && <p className="msg__reason code">{why}</p>}
        {/* Options exist only on live turns; rehydrated history shows the prompt alone. */}
        {turn.clarify.length > 0 && (
          <div className="pills">
            {turn.clarify.map((opt, i) => (
              <button key={`${opt.query}::${i}`} type="button" className="pill" disabled={busy} onClick={() => onPick(opt)}>
                {opt.label}
              </button>
            ))}
          </div>
        )}
      </AssistantShell>
    );
  }

  // A meta turn answers a question ABOUT the assistant (what it covers, what
  // changed) from verified system state, not the corpus. It is citation-
  // incapable by construction (backend returns citations=[], refused=false), so
  // it renders as plain prose: no '.cites', no '.msg__declined' register, and
  // no "No citations" fallback — there is nothing to cite and nothing declined.
  if (turn.status === "meta") {
    return (
      <AssistantShell live={turn.live}>
        <div className="msg__body">
          {/* Citation-incapable by construction — no citations/onCite passed, so
              the Markdown renders verbatim with zero stamps (INV-2). */}
          <Markdown>{turn.content}</Markdown>
        </div>
        <AuditLine turn={turn} />
      </AssistantShell>
    );
  }

  // A refusal and an out-of-scope warning share the declined register: the
  // reply is shown for what it is, then redirected — never passed off as an
  // answer. The redirects keep the full numbered-option weight (a dead end
  // shouldn't whisper its way out).
  //
  // INV-2 hinges on this: a declined turn renders no citation chips, so the
  // evidence drawer is unreachable from it. A status="error" turn isn't matched
  // here, but the backend's _refuse() empties its citations, so it falls through
  // to the cited branch below and hits the no-citations path — still no chip,
  // still no drawer. The chip (and drawer trigger) exists ONLY where citations do.
  if (turn.status === "scope_warning" || turn.refused) {
    const tag = turn.status === "scope_warning" ? "Out of scope" : "Declined · not in corpus";
    // Scope warnings always carry reason="scope_warning", which the "Out of
    // scope" tag already conveys — suppress the redundant caption so it never
    // renders the raw code. The raw-string fallback in reasonCopy stays a net
    // for unknown refusal codes on the declined-not-in-corpus path.
    const why = turn.status === "scope_warning" ? null : reasonCopy(turn.reason);
    return (
      <AssistantShell live={turn.live}>
        <div className="msg__body msg__declined">
          <span className="msg__declined-tag">{tag}</span>
          <p>{turn.content}</p>
          {/* Why it was declined — plain-language analyst copy under the tag,
              persisted across history. Text only (INV-2 holds). */}
          {why && <p className="msg__reason code">{why}</p>}
        </div>
        {/* "Related, not an answer": re-runnable queries (product names + source
            links), NOT evidence. These are inert '.pill' buttons wired to the
            same onPick as clarify — they are NEVER '.cite' chips and CANNOT open
            the evidence drawer, so INV-2 holds (a refusal surfaces no grounding).
            Live refusals only; rehydrated history leaves related []. */}
        {turn.related.length > 0 && (
          <>
            <p className="kicker">Related, not an answer</p>
            <div className="pills">
              {turn.related.map((opt, i) => (
                <button
                  key={`${opt.query}::${i}`}
                  type="button"
                  className="pill"
                  disabled={busy}
                  onClick={() => onPick(opt)}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </>
        )}
        <AuditLine turn={turn} />
      </AssistantShell>
    );
  }

  // answer / summary — a cited finding.
  const hasCitations = turn.citations.length > 0;
  // Show how the question was read ONLY when it adds information — a rewrite the
  // analyst didn't type. A quiet caption above the answer; subordinate to it.
  const interpreted =
    turn.interpretation && turn.interpretation.trim() !== turn.content.trim()
      ? turn.interpretation.trim()
      : null;
  const band = confidenceBand(turn.citations);
  return (
    <AssistantShell live={turn.live}>
      {interpreted && <p className="msg__interp">Interpreted as: {interpreted}</p>}
      <div className="msg__body">
        {/* Stamps render ONLY here (answer/summary), wired to the evidence drawer
            via onCite. INV-1: the Markdown plugin stamps a tag only when its
            (short_name,page) matches a real citation on this turn. */}
        <Markdown citations={turn.citations} onCite={onCite}>
          {turn.content}
        </Markdown>
      </div>

      {hasCitations ? (
        <>
          {/* Coarse, honest confidence — a near-threshold answer reads hedged.
              The raw score stays out of the main view (drawer only). */}
          {band && (
            <p className={`confidence confidence--${band.toLowerCase()}`}>
              <span className="confidence__dot" aria-hidden />
              {band} confidence
            </p>
          )}
          <div className="cites">
            {turn.citations.map((c, i) => (
              <CiteChip key={`${c.short_name}-${c.page}-${i}`} c={c} onSelect={onCite} />
            ))}
          </div>
          <details className="sources">
            <summary className="kicker">Sources · {turn.citations.length}</summary>
            <div className="mt-2">
              {turn.citations.map((c, i) => (
                <Reference key={`${c.short_name}-${c.page}-${i}`} n={i + 1} c={c} />
              ))}
            </div>
          </details>
        </>
      ) : (
        // Defense-in-depth for INV-1: the backend converts an ungrounded answer
        // to a refusal, so this should be unreachable — but if that ever
        // regressed, a cited surface must never silently pass off an answer with
        // no grounding as if it were sourced.
        <p className="msg__audit">No citations</p>
      )}

      {/* Feedback is gated on audit_id (meta) presence, not liveness: Tier-2
          persists audit_id, so a rehydrated answer can still be rated — a saved
          finding is no longer a dead end. */}
      {turn.meta && <AnswerFeedback auditId={turn.meta.audit_id} />}

      {turn.meta && (
        <details className="prov">
          <summary className="kicker">Provenance</summary>
          <p className="code prov__line">
            {/* model_name isn't persisted on history; omit it there rather than print "model ". */}
            {turn.meta.model_name ? `model ${turn.meta.model_name} · ` : ""}audit #{turn.meta.audit_id} · status{" "}
            {turn.status}
            {sessionId ? ` · session ${sessionId}` : ""} · turn {turn.meta.turn_id}
          </p>
        </details>
      )}
    </AssistantShell>
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
      <RecencyBadge c={c} />
      <blockquote className="ref__quote">{c.snippet}</blockquote>
      <a className="link code" style={{ fontSize: "0.76rem" }} href={safeHref(c.source_url)} target="_blank" rel="noreferrer">
        {c.source_url}
      </a>
    </div>
  );
}
