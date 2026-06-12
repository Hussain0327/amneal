"use client";

import { AnswerFeedback } from "@/components/AnswerFeedback";
import { Markdown } from "@/components/Markdown";
import { SuggestionChips, SuggestionOptions } from "@/components/Suggestions";
import type { Citation, Suggestion } from "@/lib/api";
import type { Turn } from "@/lib/turns";

// Per-status rendering of one conversation turn. The visual register encodes
// epistemic status: a cited finding gets the sealed document card and a
// references ledger; a refusal gets the oxblood stamp; smalltalk gets a quiet
// memorandum with no card at all — chat never dresses up as guidance.

export function UserTurn({ content }: { content: string }) {
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

function AuditLine({ turn }: { turn: Turn }) {
  if (!turn.meta) return null;
  return (
    <p className="code mt-3" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
      audit #{turn.meta.audit_id} · {turn.meta.model_name}
    </p>
  );
}

export function AssistantTurn({
  turn,
  sessionId,
  onPick,
  busy,
}: {
  turn: Turn;
  sessionId: string | null;
  onPick: (s: Suggestion) => void;
  busy: boolean;
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
              <button
                key={`${opt.query}::${i}`}
                type="button"
                className="opt"
                disabled={busy}
                onClick={() => onPick(opt)}
              >
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

  if (turn.status === "conversational") {
    return (
      <section className="mt-6 rise memo">
        <div className="kicker" style={{ color: "var(--ink-faint)" }}>
          Memorandum
        </div>
        <p className="memo__body">{turn.content}</p>
        <SuggestionChips heading="Continue" suggestions={turn.suggestions} onPick={onPick} busy={busy} />
        <AuditLine turn={turn} />
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
        <SuggestionOptions heading="Nearest in the corpus" suggestions={turn.suggestions} onPick={onPick} busy={busy} />
        <AuditLine turn={turn} />
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
        {/* The refusal stays a refusal — but it stops being a dead end. */}
        <SuggestionOptions heading="Nearest in the corpus" suggestions={turn.suggestions} onPick={onPick} busy={busy} />
        <AuditLine turn={turn} />
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
        {/* Partial answer: the unsupported aspects, annotated inside the
            finding — empty on purpose, not an error. */}
        {turn.unanswered.length > 0 && (
          <div className="na">
            <div className="na__tag">Not addressed · not in the corpus</div>
            <ul className="na__list">
              {turn.unanswered.map((aspect, i) => (
                <li key={i} className="na__item">
                  {aspect}
                </li>
              ))}
            </ul>
          </div>
        )}
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

      <SuggestionChips heading="Continue" suggestions={turn.suggestions} onPick={onPick} busy={busy} />

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
