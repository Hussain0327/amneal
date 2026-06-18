// One rendered turn of an Ask conversation, shared by the live page and the
// design fixtures. Live assistant turns carry clarify options and provenance;
// turns rehydrated from GET /sessions/{id} carry only content / status /
// citations and degrade cleanly (no chips, no feedback affordance).

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
  interpretation: string | null;
  meta: { model_name: string; audit_id: number; turn_id: string } | null;
}

const STATUSES: readonly string[] = [
  "answer",
  "summary",
  "clarify",
  "scope_warning",
  "refused",
  "error",
];

export function turnFromMessage(m: ChatMessage): Turn {
  const status = m.status && STATUSES.includes(m.status) ? (m.status as QueryStatus) : null;
  return {
    role: m.role,
    content: m.content,
    status,
    refused: status === "refused",
    citations: m.citations ?? [],
    clarify: [],
    interpretation: null,
    meta: null,
  };
}

export function userTurn(q: string): Turn {
  return {
    role: "user",
    content: q,
    status: null,
    refused: false,
    citations: [],
    clarify: [],
    interpretation: null,
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
    interpretation: r.interpretation ?? null,
    meta: { model_name: r.model_name, audit_id: r.audit_id, turn_id: r.turn_id },
  };
}
