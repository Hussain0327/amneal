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

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`POST ${path} → ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`);
  if (!res.ok) {
    throw new Error(`GET ${path} → ${res.status}`);
  }
  return res.json() as Promise<T>;
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
