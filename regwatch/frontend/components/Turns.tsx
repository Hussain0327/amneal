"use client";

import { AnswerFeedback } from "@/components/AnswerFeedback";
import { Markdown } from "@/components/Markdown";
import type { Citation, Suggestion } from "@/lib/api";
import type { Turn } from "@/lib/turns";
import { safeHref } from "@/lib/url";

// Ask renders as a cited chat: the user's line as a bubble, the assistant as a
// gold avatar + message. Epistemic status still governs the register — a cited
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

export function UserTurn({ content }: { content: string }) {
  return (
    <div className="chat-row chat-row--user rise">
      <div className="bubble bubble--user">{content}</div>
    </div>
  );
}

function AuditLine({ turn }: { turn: Turn }) {
  if (!turn.meta) return null;
  return (
    <p className="msg__audit code">
      audit #{turn.meta.audit_id} · {turn.meta.model_name}
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

// The assistant frame: gold RW avatar + a message column the status branches fill.
function AssistantShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="chat-row rise">
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
    return (
      <AssistantShell>
        <div className="msg__body">{turn.interpretation || turn.content}</div>
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
      <AssistantShell>
        <div className="msg__body">
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
    return (
      <AssistantShell>
        <div className="msg__body msg__declined">
          <span className="msg__declined-tag">{tag}</span>
          <p>{turn.content}</p>
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
  return (
    <AssistantShell>
      <div className="msg__body">
        <Markdown>{turn.content}</Markdown>
      </div>

      {turn.citations.length > 0 ? (
        <>
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

      {/* Thumbs only on live turns: meta (and so audit_id) is absent on turns
          rehydrated from session history, which degrade to no affordance. */}
      {turn.meta && <AnswerFeedback auditId={turn.meta.audit_id} />}

      {turn.meta && (
        <details className="prov">
          <summary className="kicker">Provenance</summary>
          <p className="code prov__line">
            model {turn.meta.model_name} · audit #{turn.meta.audit_id} · status {turn.status}
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
      <blockquote className="ref__quote">{c.snippet}</blockquote>
      <a className="link code" style={{ fontSize: "0.76rem" }} href={safeHref(c.source_url)} target="_blank" rel="noreferrer">
        {c.source_url}
      </a>
    </div>
  );
}
