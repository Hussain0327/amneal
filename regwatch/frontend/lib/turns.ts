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
  };
}

// Plain-language analyst copy for a backend reason code (QAResult.reason).
// Keeps the WHY of a decline/clarify legible without leaking the code; an
// unknown code falls back to the raw string so a new backend reason still shows
// something rather than vanishing.
const REASON_COPY: Record<string, string> = {
  low_top_score:
    "No passage scored high enough to answer this confidently — try naming the product or adding a specific detail.",
  no_product: "No product in the corpus matched this query.",
  no_matching_psg: "No product-specific guidance matched this query.",
  did_you_mean: "The product name is close to a known one — pick the intended match.",
  multi_form: "This ingredient has several dosage forms — pick one.",
  mixed_products: "The question spans multiple products — narrow it to one.",
  ambiguous_product: "More than one product matched — pick the intended one.",
  vague_input: "The question is too broad to retrieve against — add specifics.",
  brand_lookup: "Looks like a brand name — confirm the active ingredient.",
  retrieval: "Retrieval could not find supporting passages.",
  spine_unresolved: "Could not resolve the product to a known FDA application.",
  provider_error: "The model provider failed to respond.",
  empty_completion: "The model returned no answer.",
  model_refusal:
    "The retrieved passages didn't support a confident answer — try rephrasing or naming the product.",
  no_valid_citations: "The draft answer had no citation backed by a real passage.",
};

export function reasonCopy(reason: string | null): string | null {
  if (!reason) return null;
  return REASON_COPY[reason] ?? reason;
}

// Coarse, honest confidence band from the best citation score. Answers only
// survive the ~0.30 refusal threshold, so everything shown is at least
// "Moderate"; a clear-cut retrieval (>=0.55) reads "High". We deliberately show
// a BAND, not the raw float — a near-threshold answer must read visibly hedged
// in the main view, with the number reserved for the evidence drawer. Returns
// null when no score is on the wire (older/streamed turns) so nothing is faked.
export type ConfidenceBand = "High" | "Moderate";

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
  return score >= 0.55 ? "High" : "Moderate";
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
  };
}

export function assistantTurn(r: QueryResponse): Turn {
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
  };
}
