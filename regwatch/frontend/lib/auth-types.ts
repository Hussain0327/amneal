// Wire types for the routes the GO PROXY serves natively since the step-4
// auth cutover (docs/POLYGLOT_TARGET_2026-07-10.md): POST /auth/login,
// POST /auth/logout, GET /auth/me, GET /sessions, GET/DELETE /sessions/{id}.
//
// These routes left the FastAPI OpenAPI schema when their Python handlers
// were deleted, so the generated lib/api-types.ts no longer carries their
// shapes. HAND-MAINTAINED, copied verbatim from the last generated output;
// the source of truth for these shapes is now the Go handler package and its
// contract tests (go/internal/api/sessions.go wire structs,
// contract_test.go wantKeys assertions). If the Go wire ever changes, update
// here in the same PR -- there is no codegen guard for this file.

export interface UserOut {
  display_name: string;
  email: string;
  id: number;
  role: string;
}

export interface SessionSummary {
  created_at: string;
  id: string;
  message_count: number;
  title: string;
  updated_at: string;
}

export interface SessionMeta {
  created_at: string;
  id: string;
  title: string;
  updated_at: string;
}

export interface ChatMessageOut {
  audit_id: number | null;
  citations: {
    [key: string]: unknown;
  }[];
  clarify: {
    [key: string]: unknown;
  }[];
  content: string;
  created_at: string;
  id: string;
  interpretation: string | null;
  reason: string | null;
  related: {
    [key: string]: unknown;
  }[];
  role: string;
  status: string | null;
  turn_id: string;
}

export interface SessionDetailResponse {
  messages: ChatMessageOut[];
  session: SessionMeta;
}
