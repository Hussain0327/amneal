"use client";

import { memo, useDeferredValue } from "react";

import { AnswerFeedback } from "@/components/AnswerFeedback";
import { Markdown } from "@/components/Markdown";
import { RecencyBadge } from "@/components/RecencyBadge";
import type { Citation, Suggestion } from "@/lib/api";
import { dedupeCitations, splitSourcesTrailer, trailerMarkerPairs } from "@/lib/citations";
import { formatClock, formatFiled, parseApiDate } from "@/lib/time";
import { confidenceBand, confidenceTitle, nonAnswerLabel, reasonCopy, type Turn } from "@/lib/turns";
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
//
// Memoized (with AssistantTurn below): token streaming re-renders the page once
// per SSE chunk, and without memo every settled turn re-renders — including a
// full react-markdown re-parse per turn per chunk. Turn objects are append-only
// (identity-stable) and the page keeps its handlers useCallback-stable, so the
// bail-out actually holds while a draft streams.
export const UserTurn = memo(function UserTurn({ content, live }: { content: string; live: boolean }) {
  return (
    <div className={`chat-row chat-row--user${live ? " rise" : ""}`}>
      <div className="bubble bubble--user">{content}</div>
    </div>
  );
});

// The provisional streaming draft — the assistant's answer as it types, BEFORE
// citation validation. Rendered as citation-LESS Markdown so the draft reads in
// the same typography it will settle into (no wholesale reflow on settle):
// omitting citations/onCite keeps `stampable` false, so a literal [PSG, p.N]
// never becomes a clickable stamp and there are NO citation chips, evidence
// drawer, confidence band, feedback, or audit line. Those grounding affordances
// appear ONLY on a validated turn (INV-1/INV-2). The real AssistantTurn
// replaces this the instant the result frame lands.
export function ProvisionalDraft({ text }: { text: string }) {
  // Deferred so each token's setState commits cheaply and the full markdown
  // re-parse of the (growing) draft runs at deferred priority -- it may trail
  // the newest tokens by a frame, which is invisible at stream speed.
  const deferredText = useDeferredValue(text);
  return (
    <>
      <div className="msg__body msg__body--draft">
        {/* plainLinks: unvalidated output must never render a clickable gold
            anchor -- links stay inert text until the validated turn lands. */}
        <Markdown plainLinks>{deferredText}</Markdown>
      </div>
      <p className="msg__drafting code">
        <span className="msg__drafting-dot" aria-hidden />
        {"Drafting live \u2014 the verified answer follows."}
      </p>
    </>
  );
}

// The stream died mid-draft and the answer arrived over the plain /query
// fallback: the analyst watched a half-typed draft vanish, so the settled turn
// says why -- and that the shown answer was re-verified, not the dead stream's
// text. Renders nothing on the (overwhelmingly common) clean-stream path.
// Fixed slot in EVERY branch: immediately after the reply content, before any
// feedback block -- a transport footnote never interleaves with feedback.
function FallbackNote({ turn }: { turn: Turn }) {
  if (!turn.streamFellBack) return null;
  return (
    <p className="msg__fallback code">
      {"Connection dropped mid-draft \u2014 the answer was re-run over a fresh request and may differ from the draft."}
    </p>
  );
}

// The gate withdrew a provisional draft the analyst had already started
// reading (refusal, clarify, error, or dropped claims). Keyed ONLY on the
// server-declared signal - never on diffing draft text against the answer.
function WithdrawnNote({ turn }: { turn: Turn }) {
  if (!turn.draftWithdrawn) return null;
  const why =
    turn.draftWithdrawn === "partial"
      ? "some draft statements could not be verified and were dropped"
      : "it could not be verified against the cited guidance";
  return (
    <p className="msg__fallback code">
      {`The provisional draft was withdrawn \u2014 ${why}. The response below is the verified outcome.`}
    </p>
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
//
// The docket margin: on wide viewports (>=1100px, see globals.css) the avatar
// column grows into a marginal rail carrying the turn's provenance -- time
// filed, audit no, confidence dot. aria-hidden: it restates data already read
// out by the message body / audit line, and it derives from the identity-
// stable turn object so the memo'd AssistantTurn adds no per-token work.
// `turn` is optional so a caller without one (in-flight slot, bare fixtures)
// keeps the plain avatar.
function AssistantShell({
  children,
  live,
  turn,
}: {
  children: React.ReactNode;
  live: boolean;
  turn?: Turn;
}) {
  const filedMs = turn?.createdAt != null ? parseApiDate(turn.createdAt) : null;
  // Confidence marks belong ONLY to validated answers (answer/summary): gate
  // on status BEFORE scoring, so a refused/clarify turn that ever carried a
  // scored citation still shows no dot. The branch renderers already drop the
  // citation surface on those paths; the rail must agree (INV-2 defense-in-
  // depth -- a non-answer never wears a confidence mark).
  const band =
    turn && (turn.status === "answer" || turn.status === "summary")
      ? confidenceBand(turn.citations)
      : null;
  return (
    <div className={`chat-row${live ? " rise" : ""}`}>
      <div className="chat-row__margin">
        <span className="avatar" aria-hidden>
          RW
        </span>
        {turn && (filedMs !== null || turn.meta) && (
          <div className="marginalia" aria-hidden="true">
            {filedMs !== null && turn.createdAt != null && (
              <span className="marginalia__time">{formatClock(turn.createdAt)}</span>
            )}
            {turn.meta && <span className="marginalia__audit">#{turn.meta.audit_id}</span>}
            {band && (
              <span className={`marginalia__dot marginalia__dot--${band.toLowerCase()}`} />
            )}
          </div>
        )}
      </div>
      <div className="msg">{children}</div>
    </div>
  );
}

// Memoized — see the note on UserTurn: per-token draft updates must not
// re-parse every settled turn's markdown.
export const AssistantTurn = memo(function AssistantTurn({
  turn,
  sessionId,
  onPick,
  onCite,
  busy,
  threshold,
}: {
  turn: Turn;
  sessionId: string | null;
  onPick: (s: Suggestion) => void;
  onCite: (c: Citation) => void;
  busy: boolean;
  // Live refusal_score_threshold from /settings (null until it resolves),
  // grounding the confidence tooltip. A primitive, so memo's shallow compare
  // still bails out during token streaming.
  threshold: number | null;
}) {
  if (turn.status === "clarify") {
    const why = reasonCopy(turn.reason);
    const interpreted =
      turn.interpretation && turn.interpretation.trim() !== turn.content.trim()
        ? turn.interpretation.trim()
        : null;
    return (
      <AssistantShell live={turn.live} turn={turn}>
        {interpreted && <p className="msg__interp">Interpreted as: {interpreted}</p>}
        <div className="msg__body">
          {/* Clarification copy may contain Markdown, but remains citation-incapable:
              no citations/onCite props means no stamps. */}
          <Markdown>{turn.content}</Markdown>
        </div>
        {/* Why we asked instead of answered — plain-language, persisted across
            history. Text only, so INV-2 holds (no citation surface). */}
        {why && <p className="msg__reason code">{why}</p>}
        {/* Options are persisted (Tier-2), so rehydrated clarify turns keep them;
            only pre-Tier-2 legacy rows rehydrate with clarify []. Named group so
            a screen reader announces what these buttons collectively are. */}
        {turn.clarify.length > 0 && (
          <div className="pills" role="group" aria-label="Clarification options">
            {turn.clarify.map((opt, i) => (
              <button key={`${opt.query}::${i}`} type="button" className="pill" disabled={busy} onClick={() => onPick(opt)}>
                {opt.label}
              </button>
            ))}
          </div>
        )}
        <FallbackNote turn={turn} />
        <WithdrawnNote turn={turn} />
        {/* Was asking the right call? The exact signal the clarify heuristics
            need; gated on audit_id like answer feedback. */}
        {turn.meta && <AnswerFeedback auditId={turn.meta.audit_id} variant="clarify" />}
        {/* Visible audit trail: without it a clarify turn's audit id lived
            only inside the aria-hidden marginalia -- which is only honestly
            decorative when the data is also readable here. */}
        <AuditLine turn={turn} />
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
      <AssistantShell live={turn.live} turn={turn}>
        <div className="msg__body">
          {/* Citation-incapable by construction — no citations/onCite passed, so
              the Markdown renders verbatim with zero stamps (INV-2). */}
          <Markdown>{turn.content}</Markdown>
        </div>
        <FallbackNote turn={turn} />
        <WithdrawnNote turn={turn} />
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
  // evidence drawer is unreachable from it. A status="error" turn lands here
  // too — refused=true on the live wire, and turnFromMessage maps status
  // "error" back to refused on rehydration — and the backend's _refuse()
  // empties its citations, so still no chip, still no drawer. The chip (and
  // drawer trigger) exists ONLY where citations do.
  if (turn.status === "scope_warning" || turn.refused) {
    const tag = nonAnswerLabel(turn.status, turn.refused, turn.reason) ?? "Answer unavailable";
    // Scope warnings always carry reason="scope_warning", which the "Out of
    // scope" tag already conveys — suppress the redundant caption so it never
    // renders an internal code. reasonCopy also maps unknown codes to neutral
    // copy on the evidence-gap path.
    const why = turn.status === "scope_warning" ? null : reasonCopy(turn.reason);
    const interpreted =
      turn.interpretation && turn.interpretation.trim() !== turn.content.trim()
        ? turn.interpretation.trim()
        : null;
    return (
      <AssistantShell live={turn.live} turn={turn}>
        {interpreted && <p className="msg__interp">Interpreted as: {interpreted}</p>}
        <div className="msg__body msg__declined">
          <span className="msg__declined-tag">{tag}</span>
          {/* Guidance/evidence-gap prose can retain useful lists and links, but
              never receives citation props or access to the evidence drawer. */}
          <Markdown>{turn.content}</Markdown>
          {/* Why it was declined — plain-language analyst copy under the tag,
              persisted across history. Text only (INV-2 holds). */}
          {why && <p className="msg__reason code">{why}</p>}
          {/* Infrastructure faults (not evidence gaps) are usually transient:
              tell the analyst re-asking is reasonable instead of leaving a
              dead end that reads permanent. */}
          {tag === "Answer unavailable" && (
            <p className="msg__retry code">
              {"Likely transient \u2014 try the question again in a moment."}
            </p>
          )}
        </div>
        {/* "Related, not an answer": re-runnable queries (product names + source
            links), NOT evidence. These are inert '.pill' buttons wired to the
            same onPick as clarify — they are NEVER '.cite' chips and CANNOT open
            the evidence drawer, so INV-2 holds (a refusal surfaces no grounding).
            Persisted (Tier-2), so rehydrated refusals keep them; only pre-Tier-2
            legacy rows leave related []. */}
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
        <FallbackNote turn={turn} />
        <WithdrawnNote turn={turn} />
        {/* Was declining the right call? Rating a refusal is the exact
            0.30-threshold signal; gated on audit_id like answer feedback. */}
        {turn.meta && <AnswerFeedback auditId={turn.meta.audit_id} variant="declined" />}
        <AuditLine turn={turn} />
      </AssistantShell>
    );
  }

  // answer / summary -- a cited finding. Chips, the Sources count, and the
  // reference rows all consume the SAME deduped list the stamp index is built
  // from (see Markdown.tsx), so a duplicated wire citation can't make [n]
  // disagree between a stamp and its reference row. hasCitations stays on the
  // raw array: groundedness is a property of the wire data, not our display
  // dedupe.
  const hasCitations = turn.citations.length > 0;
  const deduped = dedupeCitations(turn.citations);
  // The model-authored "Sources:" bibliography duplicates the UI's own
  // reference list (built from the VALIDATED citations), so on a cited turn the
  // prose ends where the trailer begins — and the trailer's numbering is what
  // lets bare inline [n] markers resolve to real stamps. An uncited turn keeps
  // its full text: with no reference list below, stripping would erase the only
  // source hints the reply carries.
  const { prose, trailer } = hasCitations
    ? splitSourcesTrailer(turn.content)
    : { prose: turn.content, trailer: null };
  const markers = trailer ? trailerMarkerPairs(trailer) : undefined;
  // Show how the question was read ONLY when it adds information — a rewrite the
  // analyst didn't type. A quiet caption above the answer; subordinate to it.
  const interpreted =
    turn.interpretation && turn.interpretation.trim() !== turn.content.trim()
      ? turn.interpretation.trim()
      : null;
  const band = confidenceBand(turn.citations);
  return (
    <AssistantShell live={turn.live} turn={turn}>
      {interpreted && <p className="msg__interp">Interpreted as: {interpreted}</p>}
      <div className="msg__body">
        {/* Stamps render ONLY here (answer/summary), wired to the evidence drawer
            via onCite. INV-1: the Markdown plugin stamps a tag only when its
            (short_name,page) matches a real citation on this turn. */}
        <Markdown citations={turn.citations} onCite={onCite} markers={markers}>
          {prose}
        </Markdown>
      </div>

      {hasCitations ? (
        <>
          {/* Coarse, honest confidence — a near-threshold answer reads hedged.
              The raw score stays out of the main view (drawer only); the title
              grounds the band in the live refusal threshold. */}
          {band ? (
            <p
              className={`confidence confidence--${band.toLowerCase()}`}
              title={confidenceTitle(band, threshold)}
            >
              <span className="confidence__dot" aria-hidden />
              {band} confidence
              {/* The title attr is mouse-only (unreachable by keyboard, touch,
                  or SR); restate the same explanation for everyone else. */}
              <span className="sr-only">{`\u2014 ${confidenceTitle(band, threshold)}`}</span>
            </p>
          ) : (
            // Citations without scores (older rehydrated rows): state the
            // absence explicitly -- silently omitting the band would let an
            // unscored answer read no differently from a scored one. Default
            // ink-faint dot; no band modifier, so no green/amber is faked.
            <p className="confidence confidence--none">
              <span className="confidence__dot" aria-hidden />
              Confidence not recorded
            </p>
          )}
          <div className="cites">
            {deduped.map((c, i) => (
              <CiteChip key={`${c.short_name}-${c.page}-${i}`} c={c} onSelect={onCite} />
            ))}
          </div>
          <details className="sources">
            <summary className="kicker">
              Sources {"\u00b7"} {deduped.length}
            </summary>
            <div className="mt-2">
              {deduped.map((c, i) => (
                <Reference key={`${c.short_name}-${c.page}-${i}`} n={i + 1} c={c} />
              ))}
            </div>
          </details>
        </>
      ) : (
        // Defense-in-depth for INV-1: the backend converts an ungrounded answer
        // to a refusal, so this should be unreachable — but if that ever
        // regressed, a cited surface must never silently pass off an answer with
        // no grounding as if it were sourced. An anomaly this serious gets the
        // full oxblood register under its OWN class -- INV-2 tests key on
        // .msg__declined -- not a whisper of meta text.
        <div className="msg__ungrounded" role="note">
          <span className="msg__ungrounded-tag">{"Ungrounded \u2014 treat as unverified"}</span>
          <p>
            This reply arrived with no supporting citations and could not be verified against
            the guidance corpus.
          </p>
        </div>
      )}

      <FallbackNote turn={turn} />
      <WithdrawnNote turn={turn} />

      {/* Feedback is gated on audit_id (meta) presence, not liveness: Tier-2
          persists audit_id, so a rehydrated answer can still be rated — a saved
          finding is no longer a dead end. */}
      {turn.meta && <AnswerFeedback auditId={turn.meta.audit_id} />}

      {turn.meta && (
        <details className="prov">
          <summary className="kicker">Provenance</summary>
          {/* What an analyst can act on, named: the audit number (the anchor
              feedback and traceability hang off), the engine, and when it was
              filed. The raw trace id is kept for support conversations but
              shortened -- the full value rides in the title attribute. Session
              identity stays in the URL, not restated here. */}
          <dl className="prov__grid code">
            <div className="prov__row">
              <dt>Audit</dt>
              <dd>#{turn.meta.audit_id}</dd>
            </div>
            {/* model_name isn't persisted on history; omit the row there
                rather than label an empty value. */}
            {turn.meta.model_name && (
              <div className="prov__row">
                <dt>Model</dt>
                <dd>{turn.meta.model_name}</dd>
              </div>
            )}
            {/* Absolute filed time -- only when the timestamp actually parses,
                so a malformed wire date never prints as garbage. */}
            {turn.createdAt != null && parseApiDate(turn.createdAt) !== null && (
              <div className="prov__row prov__line">
                <dt>Filed</dt>
                <dd>{formatFiled(turn.createdAt)}</dd>
              </div>
            )}
            <div className="prov__row">
              <dt>Trace</dt>
              <dd title={`turn ${turn.meta.turn_id}${sessionId ? ` · session ${sessionId}` : ""}`}>
                {turn.meta.turn_id.slice(0, 8)}
              </dd>
            </div>
          </dl>
          {/* The SSE docket log this answer settled through -- the live ticker
              is ephemeral, so the folded provenance keeps the record for the
              audit-minded (history persists no frames; rehydrated turns skip). */}
          {turn.statusLog.length > 0 && (
            <ol className="prov__log">
              {turn.statusLog.map((frame, i) => (
                <li key={`${i}-${frame}`}>{frame}</li>
              ))}
            </ol>
          )}
        </details>
      )}
    </AssistantShell>
  );
});

function Reference({ n, c }: { n: number; c: Citation }) {
  return (
    <div className="ref">
      <span className="ref__no">[{n}]</span>
      <div>
        <span className="ref__src">{c.short_name}</span>
        <span className="ref__page"> · p.{c.page}</span>
      </div>
      {/* explicitEmpty: in a reference row, a missing revision date is itself
          provenance -- state it rather than render nothing. */}
      <RecencyBadge c={c} explicitEmpty />
      <blockquote className="ref__quote">{c.snippet}</blockquote>
      {/* Labeled action, not raw-URL soup; the arrow is decorative so the
          accessible name stays "Open source PDF". */}
      <a className="link code" style={{ fontSize: "0.76rem" }} href={safeHref(c.source_url)} target="_blank" rel="noreferrer">
        Open source PDF <span aria-hidden="true">{"\u2197"}</span>
      </a>
    </div>
  );
}
