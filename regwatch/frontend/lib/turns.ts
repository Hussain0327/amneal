// One rendered turn of an Ask conversation, shared by the live page and the
// design fixtures. Tier-2 history persists clarify / related / reason /
// interpretation / audit_id, so turns rehydrated from GET /sessions/{id} keep
// their analyst affordances; only model_name (and the .rise reveal) is
// live-only. Pre-Tier-2 legacy rows degrade cleanly (empty arrays, no meta).

import type {
  ChatMessage,
  Citation,
  ClarifyOption,
  QueryResponse,
  QueryStatus,
} from "./api";

export interface Turn {
  role: "user" | "assistant";
  content: string;
  status: QueryStatus | null;
  refused: boolean;
  citations: Citation[];
  clarify: ClarifyOption[];
  // "Related, not an answer" re-runnable queries surfaced beside a refusal.
  // Persisted and returned by GET /sessions/{id}, so rehydrated refusals keep
  // them; [] occurs only on pre-Tier-2 legacy rows.
  related: ClarifyOption[];
  interpretation: string | null;
  // Backend reason code for a refusal / clarify (e.g. low_top_score, no_product,
  // did_you_mean). Persisted, so rehydrated declined/clarify turns explain
  // themselves too. Rendered as plain-language copy; INV-2-safe (text only).
  reason: string | null;
  // True only for turns that arrived LIVE this session — rehydrated history is
  // false so a reopened conversation opens static (no .rise reveal).
  live: boolean;
  meta: { model_name: string; audit_id: number; turn_id: string } | null;
  // When this turn was filed, ISO 8601. Rehydrated turns carry the server's
  // persisted created_at; live turns stamp client time (see userTurn). null
  // only if the wire ever omits it -- render nothing rather than fake a date.
  createdAt: string | null;
  // Server row id from history; null on live turns (the /query wire has no
  // message id -- meta.turn_id is the live identity).
  id: string | null;
  // SSE status frames the run settled through, captured client-side while the
  // query was in flight. History persists neither stream field -- the ticker
  // is ephemeral by design -- so rehydrated turns carry []/false (correct, not
  // a data gap).
  statusLog: string[];
  // True when the stream died mid-draft and the answer was re-fetched over the
  // plain /query fallback: the settled turn owes the analyst an explanation
  // for the draft that vanished mid-type.
  streamFellBack: boolean;
  // Server-declared: a provisional live-draft frame painted, then the gate
  // withdrew or partially dropped it ("refused" | "clarify" | "error" |
  // "meta" | "scope_warning" | "partial"). null on every turn that never
  // streamed a draft (including all rehydrated history). Never inferred by
  // diffing draft text against the answer -- only the server's own signal.
  draftWithdrawn: string | null;
}

const STATUSES: readonly string[] = [
  "answer",
  "summary",
  "clarify",
  "scope_warning",
  "meta",
  "refused",
  "error",
];

export function turnFromMessage(m: ChatMessage): Turn {
  const status = m.status && STATUSES.includes(m.status) ? (m.status as QueryStatus) : null;
  // Tier-2: history now persists reason / interpretation / clarify / related /
  // audit_id, so a rehydrated turn keeps its analyst affordances. model_name is
  // NOT persisted, so meta exists iff audit_id does (gates feedback) and carries
  // an empty model_name — the audit line / provenance render around that.
  return {
    role: m.role,
    content: m.content,
    status,
    // The wire shape has no refused flag. The backend persists provider-failure
    // turns as status="error" with refused=True live, so mirror that here —
    // otherwise a rehydrated error turn falls out of the declined register and
    // renders dressed as an answer (INV-2 drift between live and reload).
    refused: status === "refused" || status === "error",
    citations: m.citations ?? [],
    clarify: m.clarify ?? [],
    related: m.related ?? [],
    interpretation: m.interpretation ?? null,
    reason: m.reason ?? null,
    // Rehydrated history is static: no .rise reveal on a reopened conversation.
    live: false,
    meta:
      m.audit_id != null
        ? { model_name: "", audit_id: m.audit_id, turn_id: m.turn_id }
        : null,
    createdAt: m.created_at ?? null,
    id: m.id ?? null,
    // The stream lifecycle is not persisted -- a reopened conversation has no
    // ticker history and never fell back "again".
    statusLog: [],
    streamFellBack: false,
    // Same reasoning: no live-draft channel exists on rehydrated history.
    draftWithdrawn: null,
  };
}

// Plain-language analyst copy for a backend reason code (QAResult.reason).
// Keeps the WHY of a decline/clarify legible without leaking the code; an
// unknown code falls back to neutral copy so internal identifiers never leak.
const REASON_COPY: Record<string, string> = {
  low_top_score:
    "No passage scored high enough to answer this confidently — try naming the product or adding a specific detail.",
  no_product: "The product could not be identified confidently from this query.",
  no_matching_psg: "No product-specific guidance matched this query.",
  did_you_mean: "The product name is close to a known one — pick the intended match.",
  multi_form: "This ingredient has several dosage forms — pick one.",
  mixed_products: "The question spans multiple products — narrow it to one.",
  ambiguous_product: "More than one product matched — pick the intended one.",
  vague_input: "The question is too broad to retrieve against — add specifics.",
  brand_lookup: "Looks like a brand name — confirm the active ingredient.",
  retrieval: "Retrieval could not find supporting passages.",
  spine_unresolved: "Could not resolve the product to a known FDA application.",
  provider_error: "The answer service did not respond.",
  empty_completion: "The answer service returned no usable response.",
  catalog_error: "The guidance catalog could not be checked right now.",
  pipeline_error: "An internal processing step could not be completed.",
  upstream_error: "The answer service could not be reached.",
  audit_error: "The answer could not be safely recorded.",
  malformed_structure: "The model response could not be validated.",
  material_drop: "The draft omitted a material qualifier, so it was withheld.",
  model_refusal:
    "The retrieved passages didn't support a confident answer — try rephrasing or naming the product.",
  no_valid_citations: "The draft could not be verified against the retrieved passages.",
};

export function reasonCopy(reason: string | null): string | null {
  if (!reason) return null;
  return REASON_COPY[reason] ?? "The request could not be completed as expected.";
}

// Neutral outcome labels for non-answer turns. A refused result means the
// available evidence could not support an answer; status="error" (or one of
// the known infrastructure failures) means the answer itself was not
// available. Every code here is a system fault, not an epistemic judgement --
// labelling an outage "Evidence gap" would wrongly tell the analyst the corpus
// is silent (~12% of prod refusals are malformed_structure). Keep this shared
// by the visible turn and the Ask live-region so sighted and screen-reader
// users receive the same distinction.
const ANSWER_UNAVAILABLE_REASONS = new Set([
  "provider_error",
  "empty_completion",
  "upstream_error",
  "catalog_error",
  "pipeline_error",
  "audit_error",
  "malformed_structure",
]);

export type NonAnswerLabel = "Out of scope" | "Evidence gap" | "Answer unavailable";

export function nonAnswerLabel(
  status: QueryStatus | null,
  refused: boolean,
  reason: string | null,
): NonAnswerLabel | null {
  if (status === "scope_warning") return "Out of scope";
  if (status === "error" || (reason != null && ANSWER_UNAVAILABLE_REASONS.has(reason))) {
    return "Answer unavailable";
  }
  if (refused || status === "refused") return "Evidence gap";
  return null;
}

// Coarse, honest confidence band from the best citation score. Answers only
// survive the ~0.30 refusal threshold, so everything shown is at least
// "Moderate"; a clear-cut retrieval (>=0.55) reads "High". We deliberately show
// a BAND, not the raw float — a near-threshold answer must read visibly hedged
// in the main view, with the number reserved for the evidence drawer. Returns
// null when no score is on the wire (older/streamed turns) so nothing is faked.
export type ConfidenceBand = "High" | "Moderate";

// The High/Moderate cut on the top passage score. Shared by confidenceBand and
// confidenceTitle so the band and the tooltip explaining it can never drift.
const HIGH_CONFIDENCE_SCORE = 0.55;

export function topScore(citations: Citation[]): number | null {
  let best: number | null = null;
  for (const c of citations) {
    if (typeof c.score === "number" && (best === null || c.score > best)) best = c.score;
  }
  return best;
}

export function confidenceBand(citations: Citation[]): ConfidenceBand | null {
  const score = topScore(citations);
  if (score === null) return null;
  return score >= HIGH_CONFIDENCE_SCORE ? "High" : "Moderate";
}

/**
 * Plain-language grounding for a confidence band (the .confidence tooltip).
 * "Moderate" is only meaningful against the floor the answer had to clear, so
 * when the live refusal_score_threshold is known it is named exactly; before
 * /settings resolves (threshold null) the copy degrades to the bare cut
 * rather than fake a number.
 */
export function confidenceTitle(band: ConfidenceBand, threshold: number | null): string {
  if (band === "High") return `Top passage score at or above ${HIGH_CONFIDENCE_SCORE}`;
  return threshold != null
    ? `Above the refusal threshold (${threshold.toFixed(2)}), below ${HIGH_CONFIDENCE_SCORE}`
    : `Below ${HIGH_CONFIDENCE_SCORE}`;
}

export function userTurn(q: string): Turn {
  return {
    role: "user",
    content: q,
    status: null,
    refused: false,
    citations: [],
    clarify: [],
    related: [],
    interpretation: null,
    reason: null,
    live: true,
    meta: null,
    // Client clock, not server time: honest-but-local until a reload rehydrates
    // the server's created_at.
    createdAt: new Date().toISOString(),
    id: null,
    statusLog: [],
    streamFellBack: false,
    draftWithdrawn: null,
  };
}

// What the page-level run() observed on the wire while this turn streamed.
// Optional so fixtures and non-streaming callers stay valid with one argument.
interface StreamTrace {
  statusLog?: string[];
  streamFellBack?: boolean;
  draftWithdrawn?: string | null;
}

export function assistantTurn(r: QueryResponse, trace: StreamTrace = {}): Turn {
  return {
    role: "assistant",
    content: r.answer,
    status: r.status,
    refused: r.refused || r.status === "refused",
    citations: r.citations,
    clarify: r.clarify,
    related: r.related ?? [],
    interpretation: r.interpretation ?? null,
    reason: r.reason ?? null,
    // Arrived live this turn: eligible for the .rise reveal.
    live: true,
    meta: { model_name: r.model_name, audit_id: r.audit_id, turn_id: r.turn_id },
    // Client clock, not server time: honest-but-local until a reload rehydrates
    // the server's created_at.
    createdAt: new Date().toISOString(),
    id: null,
    statusLog: trace.statusLog ?? [],
    streamFellBack: trace.streamFellBack ?? false,
    draftWithdrawn: trace.draftWithdrawn ?? null,
  };
}
