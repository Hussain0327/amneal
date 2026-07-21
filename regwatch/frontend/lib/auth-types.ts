// Wire types for the routes the GO PROXY serves natively since the step-4
// cutovers (docs/POLYGLOT_TARGET_2026-07-10.md): POST /auth/login,
// POST /auth/logout, GET /auth/me, GET /sessions, GET/DELETE /sessions/{id},
// and since C2 also GET /products and GET /settings (see the Step-4C section
// at the bottom).
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

// ---- Step-4C additions: GET /products + GET /settings (Go-served) ----
// Same contract as above: hand-maintained mirrors of the shapes that left the
// generated api-types.ts when the Python products/settings/feedback handlers
// were deleted (C2). Source of truth: go/internal/api/products.go
// (productRecord/productsResponse) and settings.go (publicSettings), pinned
// by contract_c_test.go. POST /feedback needs no entry -- api.ts discards its
// response body.

export interface ProductRecord {
  active_ingredient: string;
  company_status: string | null;
  dosage_form: string | null;
  id: number | null;
  normalized_name: string;
  rld_application_number: string | null;
  rld_name: string | null;
  route: string | null;
  source: string;
  source_url: string | null;
  stripped_name: string;
}

export interface ProductsResponse {
  count: number;
  products: ProductRecord[];
}

export interface PublicSettings {
  company_name: string;
  embedding_provider: string;
  llm_model: string;
  llm_provider: string;
  refusal_score_threshold: number;
  retrieval_top_k: number | null;
}
