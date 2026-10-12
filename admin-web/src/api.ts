// FastAPI(escrow/admin/app.py) 와 같은 주소에서 돈다 — 쿠키 세션 + X-Requested-With 헤더(CSRF 막이)
export class Unauthorized extends Error {}
export class ApiError extends Error {}

export async function api<T = any>(path: string, opt: RequestInit = {}): Promise<T> {
  const r = await fetch(path, {
    ...opt,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "escrow", ...(opt.headers || {}) },
  });
  if (r.status === 401) throw new Unauthorized();
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(j.detail || `오류 ${r.status}`);
  return j as T;
}

export const post = <T = any>(p: string, b: unknown = {}) => api<T>(p, { method: "POST", body: JSON.stringify(b) });
export const put = <T = any>(p: string, b: unknown = {}) => api<T>(p, { method: "PUT", body: JSON.stringify(b) });

export type Stats = {
  active: number; today: number; done_total: number; pending_approvals: number; held_approvals: number;
  payouts_to_send: number; unmatched_in: number; disputes: number; rooms_free: number;
  ledger: string; ledger_micro: number; wallet: string | null; wallet_micro: number | null; fees: string;
};
