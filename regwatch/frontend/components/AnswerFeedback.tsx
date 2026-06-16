"use client";

import { useRef, useState } from "react";

import { sendFeedback } from "@/lib/api";

type Phase = "idle" | "saving" | "saved" | "error";

// Thumbs on an answer turn. The rating POSTs immediately on either thumb — it
// is never held hostage to the optional comment; thumbs-down then opens a
// one-line note whose send simply re-submits (the server upserts per
// audit+user, so the note replaces the bare -1, and re-rating replaces both).
// Callers render this only when the turn carries an audit_id: restored
// history messages don't have one, and they quietly get no affordance rather
// than a dead control.
export function AnswerFeedback({ auditId }: { auditId: number }) {
  const [rating, setRating] = useState<1 | -1 | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [note, setNote] = useState("");
  const [noteSent, setNoteSent] = useState(false);
  // The last thing we tried to POST, so an explicit Retry can re-fire exactly
  // it after a transient failure (rather than relying on a re-click).
  const lastAttempt = useRef<{ rating: 1 | -1; comment: string | null } | null>(null);

  function submit(nextRating: 1 | -1, comment: string | null) {
    lastAttempt.current = { rating: nextRating, comment };
    void (async () => {
      setPhase("saving");
      try {
        await sendFeedback(auditId, nextRating, comment);
        setRating(nextRating);
        if (comment !== null) setNoteSent(true);
        setPhase("saved");
      } catch {
        setPhase("error");
      }
    })();
  }

  function rate(next: 1 | -1) {
    // Ignore a no-op re-click of the already-saved thumb (buttons stay enabled
    // after success); a real switch (next !== rating) still fires.
    if (phase === "saving" || next === rating) return;
    setNoteSent(false);
    if (next === 1) setNote("");
    submit(next, null);
  }

  function onNoteSubmit(e: React.FormEvent) {
    e.preventDefault();
    const text = note.trim();
    if (!text || phase === "saving" || rating !== -1) return;
    submit(-1, text);
  }

  function retry() {
    if (phase === "saving" || !lastAttempt.current) return;
    submit(lastAttempt.current.rating, lastAttempt.current.comment);
  }

  const status =
    phase === "saving"
      ? "recording…"
      : phase === "error"
        ? "Could not record."
        : phase === "saved"
          ? "✓ Noted — thank you."
          : null;

  return (
    <div className="mt-5">
      <div className="fb">
        <span className="kicker" style={{ color: "var(--ink-faint)" }}>
          Assess
        </span>
        <button
          type="button"
          className={`fb__btn${rating === 1 ? " fb__btn--on" : ""}`}
          aria-pressed={rating === 1}
          disabled={phase === "saving"}
          onClick={() => rate(1)}
        >
          <span aria-hidden>↑</span> Helpful
        </button>
        <button
          type="button"
          className={`fb__btn${rating === -1 ? " fb__btn--on" : ""}`}
          aria-pressed={rating === -1}
          disabled={phase === "saving"}
          onClick={() => rate(-1)}
        >
          <span aria-hidden>↓</span> Not helpful
        </button>
        {status && (
          <span
            className="code fb__status"
            role="status"
            style={{ color: phase === "error" ? "var(--oxblood)" : "var(--ink-soft)" }}
          >
            {status}
          </span>
        )}
        {phase === "error" && (
          <button type="button" className="fb__btn" onClick={retry}>
            Retry
          </button>
        )}
      </div>

      {rating === -1 && !noteSent && (
        <form className="fb__note" onSubmit={onNoteSubmit}>
          <input
            className="field"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="What was off? · optional"
            aria-label="What was off? Optional note."
          />
          <button className="btn btn--ghost" type="submit" disabled={!note.trim() || phase === "saving"}>
            Send note
          </button>
        </form>
      )}
    </div>
  );
}
