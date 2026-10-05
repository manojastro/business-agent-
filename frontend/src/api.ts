// Thin API client: cookie session + CSRF header on mutations, structured errors.

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

let csrfToken = "";
export const setCsrf = (t: string) => {
  csrfToken = t;
};

async function request<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const init: RequestInit = { method, credentials: "include", headers: { ...headers } };
  if (body !== undefined) {
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (method !== "GET") (init.headers as Record<string, string>)["X-CSRF-Token"] = csrfToken;
  const res = await fetch(`/api/v1${path}`, init);
  if (!res.ok) {
    let code = "http_error";
    let message = res.statusText;
    let details: unknown;
    try {
      const j = await res.json();
      code = j.error?.code ?? code;
      message = j.error?.message ?? message;
      details = j.error?.details;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, message, details);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T,>(p: string) => request<T>("GET", p),
  post: <T,>(p: string, b?: unknown, h?: Record<string, string>) => request<T>("POST", p, b ?? {}, h),
  patch: <T,>(p: string, b?: unknown) => request<T>("PATCH", p, b ?? {}),
  del: <T,>(p: string) => request<T>("DELETE", p),
};

// ---------- types ----------
export interface Tenant { id: string; slug: string; name: string; role: string; kind: string }
export interface SessionInfo {
  user: { id: string; email: string; display_name: string };
  active_tenant: Tenant;
  tenants: Tenant[];
  csrf_token: string;
  model_mode: string;
  demo_as_of: string;
}
export interface Window { start: string; end: string }
export interface Budgets {
  queries_used: number; queries_max: number; steps_used: number; steps_max: number;
  tokens_used: number; cost_used: string; cost_max: string; hypotheses_max: number; cost_note: string;
}
export interface Hypothesis { key: string; statement: string; kind: string; status: string; rationale: string; evidence_ids: string[] }
export interface Driver { label: string; confidence: string; explanation: string }
export interface Investigation {
  id: string; question: string; status: string; metric_key: string | null; metric_version: number | null;
  baseline_window: Window | null; current_window: Window | null; as_of: string; timezone: string;
  source_watermark: string | null; model: Record<string, string>; simulation_label: string | null;
  budgets: Budgets; pending_question: PendingQuestion | null; primary_driver: Driver | null;
  error: Record<string, string> | null; cancel_requested: boolean; incomplete_data: boolean;
  owner: { id: string; display_name?: string }; created_at: string; completed_at: string | null;
  hypotheses?: Hypothesis[]; report?: { id: string; status: string; current_version: number } | null;
  permissions?: { can_review: boolean; can_cancel: boolean; can_rerun: boolean; can_view_sql: boolean };
}
export interface PendingQuestion {
  type: "metric" | "incomplete_data"; question: string; options: { value: string; label: string }[]; issues?: string[];
}
export interface Step { id: number; node: string; kind: string; title: string; rationale: string; detail: Record<string, unknown>; created_at: string }
export interface EvidenceSummary {
  id: string; label: string; kind: string; row_count: number; result_hash: string; plan_hash: string | null;
  execution_ms: number; source_watermark: string | null; metric_key: string | null; metric_version: number | null; created_at: string;
  investigation_id: string;
}
export interface EvidenceDetail extends EvidenceSummary {
  result: Record<string, unknown>; compiled_sql: string | null; params: Record<string, unknown>;
}
export interface ReportItem { key: string; wording: string; claim_type: string; evidence_ids: string[]; numeric: Record<string, unknown>[]; verification: Record<string, unknown>[] }
export interface ReportContent {
  title: string; question: string; summary: string; version: number; revision_note: string;
  simulation_label: string | null; primary_driver: Driver; model: Record<string, string>;
  scope: { metric: { key: string; name: string; version: number; formula: string }; baseline_window: Window; current_window: Window; timezone: string; as_of: string; source_watermark: string | null; currency: string };
  headline: Record<string, string | boolean | null> | null;
  materiality: Record<string, unknown> | null;
  findings: ReportItem[]; hypotheses: ReportItem[]; limitations: ReportItem[]; recommendations: ReportItem[];
  rejected_claims: { key: string; wording: string; origin: string; reasons: { code: string; detail: string }[] }[];
  hypothesis_table: Hypothesis[];
  charts: {
    components?: { evidence_id: string; points: { label: string; value: string }[]; total: string };
    dimension?: { evidence_id: string; dimension: string; points: { label: string; value: string; small: boolean }[] };
    daily?: { evidence_id: string; points: { day: string; value: string }[] };
  };
  evidence_index: { id: string; label: string; kind: string; row_count: number; result_hash: string }[];
}
export interface Report {
  id: string; investigation_id: string; status: string; current_version: number; version: number;
  content: ReportContent; content_hash: string;
  versions: { version: number; created_at: string; revision_note: string; content_hash: string }[];
  decisions: { decision: string; comment: string; reviewer: string; created_at: string }[];
}

export const fmtMoney = (v: string | number | null | undefined) => {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  const s = `₹${Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return n < 0 ? `-${s}` : s;
};
export const fmtNum = (v: string | number | null | undefined, unit?: string) => {
  if (v === null || v === undefined) return "—";
  if (unit === "currency") return fmtMoney(v);
  if (unit === "ratio") return `${(Number(v) * 100).toFixed(2)}%`;
  return Number(v).toLocaleString("en-IN");
};
export const fmtDate = (s: string | null | undefined) => (s ? new Date(s).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "—");

export const unitOf = (metricKey: string | null | undefined) =>
  metricKey === "order_count" ? "count" : metricKey === "refund_rate" ? "ratio" : "currency";

export const newIdempotencyKey = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
