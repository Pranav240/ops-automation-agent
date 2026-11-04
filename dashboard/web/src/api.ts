// Calls to the dashboard API. The token stays in this tab only and is sent as a bearer token.

const TOKEN_KEY = "ops-dashboard-token";

export type Scope = "viewer" | "approver";
export type Decision = "approve" | "reject" | "edit";
export type Args = Record<string, string | number | null>;

export interface Run {
  run_id: string;
  source: string;
  request_id: string;
  ticket_key: string | null;
  status: string | null;
  action: string | null;
  amount: number | null;
  sent_to_approval: boolean;
  approved_by: string | null;
  executed: boolean;
  llm_calls: number | null;
  cost_usd: number | null;
  latency_s: number | null;
  model: string | null;
  created_at: string;
}

export interface Pending {
  action: string | null;
  args: Args;
  reply: string;
  reasons: string[];
  critique: { ok: boolean; issues: string[] };
}

export interface Step {
  node: string;
  started_at: string;
  seconds: number | null;
  output: Record<string, unknown>;
}

export interface RunDetail {
  run: Run;
  trace_id: string;
  customer_email: string | null;
  request_text: string | null;
  steps: Step[];
  pending: Pending | null;
  outcome: { status: string; reply: string; executed: boolean; args: Args } | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export function savedToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Blocked storage only means signing in again after a reload.
  }
}

function errorText(detail: unknown, fallback: string): string {
  return typeof detail === "string" ? detail : fallback;
}

async function call<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const response = await fetch(path, { ...init, headers });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, errorText(body?.detail, `request failed (${response.status})`));
  }
  return body as T;
}

export const api = {
  me: (token: string) => call<{ scope: Scope }>(token, "/api/me"),
  runs: (token: string, status?: string) =>
    call<Run[]>(token, `/api/runs${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  run: (token: string, runId: string) =>
    call<RunDetail>(token, `/api/runs/${encodeURIComponent(runId)}`),
  decide: (token: string, runId: string, decision: Decision, args?: Args) =>
    call<{ sent: Decision }>(token, `/api/runs/${encodeURIComponent(runId)}/decision`, {
      method: "POST",
      body: JSON.stringify(args ? { decision, args } : { decision }),
    }),
};
