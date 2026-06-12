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

// --- White Paper populator (POST /whitepaper) -------------------------------
// Cells are tri-state. manual cells are ALWAYS analyst_input_required with
// evidence attached and no generated value; verified_absent means the source
// was queried successfully and the record is genuinely absent (renders "No",
// query recorded in evidence) — a failed or ambiguous lookup never says "No".

export type WhitepaperCellMode = "auto" | "evidence_only" | "manual";
export type WhitepaperCellStatus = "populated" | "analyst_input_required" | "verified_absent";

export interface WhitepaperEvidence {
  source: string;
  locator: string;
  source_url: string | null;
  fetched_at: string | null;
  page: number | null;
  section: string | null;
  snippet: string | null;
}

export interface WhitepaperCell {
  id: string;
  label: string;
  mode: WhitepaperCellMode;
  status: WhitepaperCellStatus;
  value: string | null;
  evidence: WhitepaperEvidence[];
  note: string | null;
}

export interface WhitepaperSectionData {
  title: string;
  cells: WhitepaperCell[];
}

export interface WhitepaperSpine {
  application_number: string;
  // The backend can also resolve BLA inputs (Drugs@FDA / Orange Book carry them).
  application_type: "NDA" | "ANDA" | "BLA";
  ingredient: string;
  normalized_name: string;
  product_numbers: string[];
  setid: string | null;
  warnings: string[];
}

export interface WhitepaperResponse {
  spine: WhitepaperSpine;
  sections: WhitepaperSectionData[];
  warnings: string[];
  audit_id: number;
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

export function buildWhitepaper(
  rldName: string,
  applicationNumber: string,
): Promise<WhitepaperResponse> {
  // A 422 here is the resolution-failure contract: detail explains what WAS
  // found — surface it verbatim, never retry with a guess.
  return postJSON<WhitepaperResponse>("/whitepaper", {
    rld_name: rldName,
    application_number: applicationNumber,
  });
}

// Content-Disposition: attachment; filename=whitepaper_020503.docx — also
// tolerates the quoted and RFC 5987 (filename*=UTF-8''…) forms.
function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"+|"+$/g, ""));
    } catch {
      // malformed encoding — fall through to the plain form
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : null;
}

// The body is {result: <the exact JSON object buildWhitepaper returned>}: the
// server verifies result.audit_id is the caller's own white-paper run and
// renders the .docx FROM that payload — no re-populate, so the document can
// never silently differ from what the analyst reviewed. The 200 is the .docx
// itself: read it as a blob and hand it to the browser as a download under
// the server's Content-Disposition filename. Error bodies are JSON, so they
// go through the shared handler — same 401 gate and detail parsing as every
// other call.
export async function downloadWhitepaperDocx(result: WhitepaperResponse): Promise<void> {
  const path = "/whitepaper/docx";
  const res = await fetch(`${apiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ result }),
    credentials: "include",
  });
  if (!res.ok) {
    await handle<never>(res, "POST", path, true); // always throws
    return;
  }
  const blob = await res.blob();
  const fallback = `whitepaper_${result.spine.application_number}.docx`;
  const filename = filenameFromDisposition(res.headers.get("content-disposition")) ?? fallback;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoking in the same tick aborts the download in WebKit — defer until the
  // browser has had a chance to start streaming the blob.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Answer feedback: rating is exactly +1 (helpful) or -1 (not helpful); the
// server upserts per (audit_id, user) so re-rating — or re-sending with a
// comment attached — replaces the previous row rather than stacking. A 404
// means the audit row isn't the caller's own qa answer; surfaced like any
// other ApiError. Response body is ignored on purpose: success is the signal.
export async function sendFeedback(
  auditId: number,
  rating: 1 | -1,
  comment: string | null = null,
): Promise<void> {
  await postJSON<unknown>("/feedback", { audit_id: auditId, rating, comment });
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
