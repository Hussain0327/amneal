// Typed client for the RegWatch FastAPI backend. Types mirror the Pydantic
// response models in src/regwatch/api/main.py exactly — field names are the
// contract, do not rename without changing the API.

// Resolve the API base at call time. When the page is served from a REMOTE
// origin (e.g. a cloudflared tunnel) we ALWAYS use the same-origin "/api" path
// that next.config.mjs proxies to the backend — a localhost base would point at
// the visitor's own machine, not the server. NEXT_PUBLIC_API_BASE is honored
// only when actually running on localhost (for direct-call local dev).
function apiBase(): string {
  if (typeof window !== "undefined") {
    const host = window.location.hostname;
    if (host !== "localhost" && host !== "127.0.0.1") return "/api";
  }
  const configured = process.env.NEXT_PUBLIC_API_BASE;
  return configured && configured.length > 0 ? configured : "/api";
}

export interface Citation {
  short_name: string;
  page: number;
  chunk_id: string;
  doc_id: number;
  version_id: number;
  source_url: string;
  snippet: string;
}

export interface ClarifyOption {
  label: string;
  query: string;
  filters: Record<string, string> | null;
}

export type QueryStatus = "answer" | "summary" | "clarify" | "scope_warning" | "refused";

export interface QueryResponse {
  answer: string;
  citations: Citation[];
  refused: boolean;
  model_name: string;
  audit_id: number;
  session_id: string;
  turn_id: string;
  status: QueryStatus;
  interpretation: string | null;
  clarify: ClarifyOption[];
}

export interface AssembleResponse {
  markdown: string;
  sections: Record<string, unknown>;
  refused: boolean;
}

export interface AlertRecord {
  product_id: number;
  active_ingredient: string;
  listing_appl_no: string;
  listing_psg_type: string;
  psg_document_id: number;
  psg_version_id: number;
  captured_at: string;
  diff_summary: string | null;
  confidence: number;
  rationale: string;
  source_url: string;
}
export interface WatchLatest {
  count: number;
  alerts: AlertRecord[];
}

export interface ProductRecord {
  id: number | null;
  active_ingredient: string;
  normalized_name: string;
  stripped_name: string;
  dosage_form: string | null;
  route: string | null;
  rld_name: string | null;
  rld_application_number: string | null;
  company_status: string | null;
  source: string;
  source_url: string | null;
}
export interface ProductsResponse {
  count: number;
  products: ProductRecord[];
}

export interface PublicSettings {
  embedding_provider: string;
  llm_provider: string;
  llm_model: string;
  retrieval_top_k: number | null;
  refusal_score_threshold: number;
  company_name: string;
}

export interface User {
  id: number;
  email: string;
  display_name: string;
  role: string;
}

export interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface ChatMessage {
  id: string;
  turn_id: string;
  role: "user" | "assistant";
  content: string;
  status: string | null;
  citations: Citation[];
  created_at: string;
}

export interface SessionDetail {
  session: { id: string; title: string; created_at: string; updated_at: string };
  messages: ChatMessage[];
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// AuthProvider registers a callback here, so a 401 from ANY protected call
// (everything except /auth/login) drops client auth state in one place; the
// provider then routes to /login.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

async function handle<T>(res: Response, method: string, path: string, gate: boolean): Promise<T> {
  if (res.status === 401 && gate) {
    onUnauthorized?.();
    throw new ApiError(401, "authentication required");
  }
  if (!res.ok) {
    let detail = "";
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // non-JSON error body
    }
    throw new ApiError(res.status, detail || `${method} ${path} → ${res.status}`);
  }
  if (res.status === 204) {
    return undefined as unknown as T;
  }
  return res.json() as Promise<T>;
}

// credentials: "include" — auth rides in the HttpOnly session cookie. The
// same-origin /api proxy would include it by default, but the direct-call dev
// mode (NEXT_PUBLIC_API_BASE on localhost) is cross-origin and would silently
// drop the cookie without this; the backend's CORS allows credentials.
async function postJSON<T>(path: string, body: unknown, gate = true): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
  return handle<T>(res, "POST", path, gate);
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, { credentials: "include" });
  return handle<T>(res, "GET", path, true);
}

async function deleteJSON(path: string): Promise<void> {
  const res = await fetch(`${apiBase()}${path}`, { method: "DELETE", credentials: "include" });
  await handle<void>(res, "DELETE", path, true);
}

export async function login(email: string, password: string): Promise<User> {
  // gate=false: a 401 here means bad credentials, not an expired session.
  const data = await postJSON<{ user: User }>("/auth/login", { email, password }, false);
  return data.user;
}

export async function logout(): Promise<void> {
  const res = await fetch(`${apiBase()}/auth/logout`, {
    method: "POST",
    credentials: "include",
  });
  await handle<void>(res, "POST", "/auth/logout", false);
}

export async function me(): Promise<User> {
  const data = await getJSON<{ user: User }>("/auth/me");
  return data.user;
}

export function listSessions(): Promise<{ sessions: SessionSummary[] }> {
  return getJSON<{ sessions: SessionSummary[] }>("/sessions");
}

export function getSession(sessionId: string): Promise<SessionDetail> {
  return getJSON<SessionDetail>(`/sessions/${encodeURIComponent(sessionId)}`);
}

export function deleteSession(sessionId: string): Promise<void> {
  return deleteJSON(`/sessions/${encodeURIComponent(sessionId)}`);
}

export function askQuery(
  question: string,
  filters: Record<string, string> | null = null,
  session_id: string | null = null,
): Promise<QueryResponse> {
  return postJSON<QueryResponse>("/query", { question, filters, session_id });
}

export function assemble(
  active_ingredient: string,
  dosage_form: string | null = null,
  rld: string | null = null,
): Promise<AssembleResponse> {
  return postJSON<AssembleResponse>("/assemble", { active_ingredient, dosage_form, rld });
}

export function watchLatest(): Promise<WatchLatest> {
  return getJSON<WatchLatest>("/watch/latest");
}

export function listProducts(): Promise<ProductsResponse> {
  return getJSON<ProductsResponse>("/products");
}

export function getPublicSettings(): Promise<PublicSettings> {
  return getJSON<PublicSettings>("/settings");
}
