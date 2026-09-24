import { useCallback, useEffect, useState } from "react";

const BASE = "/api";

export interface SystemStatus {
  environment: string;
  paper_mode: boolean;
  allow_live_trading: boolean;
  order_kill_switch: boolean;
  review_delay_seconds: number;
  limits: {
    max_order_size_usd: number;
    max_exposure_per_market_usd: number;
    max_exposure_global_usd: number;
  };
}

export interface HealthDeps {
  status: string;
  checks: Record<string, string>;
}

export interface WalletItem {
  id: number;
  address: string;
  label: string | null;
  approval_state: string;
  is_approved: boolean;
  is_sample: boolean;
  approved_at: string | null;
  created_at: string | null;
  composite_score: number | null;
  score_verdict: string | null;
  score_computed_at: string | null;
}

export interface PaperOrderSummary {
  status: string;
  miss_reason: string | null;
  fill_price: number | null;
  filled_size: number | null;
  fee: number | null;
  t2_decided_at: string | null;
}

export interface SignalItem {
  id: number;
  wallet_id: number;
  wallet_address: string;
  wallet_label: string | null;
  market_id: number;
  market_question: string;
  side: string;
  outcome: string;
  source_price: number;
  status: string;
  t0_traded_at: string | null;
  t1_detected_at: string | null;
  created_at: string | null;
  paper_order: PaperOrderSummary | null;
}

export interface PositionItem {
  id: number;
  wallet_id: number;
  wallet_address: string;
  wallet_label: string | null;
  market_id: number;
  market_question: string;
  market_closed: boolean;
  outcome: string;
  quantity: number;
  avg_price: number;
  realized_pnl: number;
  settled_at: string | null;
  updated_at: string | null;
}

export interface PositionsResponse {
  items: PositionItem[];
  count: number;
  totals: {
    open_count: number;
    open_cost_usd: number;
    realized_pnl_usd: number;
    settled_count: number;
  };
}

export interface ApprovalItem {
  entry_id: number;
  wallet_id: number;
  address: string;
  label: string | null;
  queued_at: string | null;
  score: {
    composite_score: number | null;
    profit_factor: number | null;
    profit_factor_90d: number | null;
    gross_profit_90d: number | null;
    gross_loss_90d: number | null;
    realized_pnl_90d: number | null;
    window_days: number;
  } | null;
}

async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method,
    ...(body !== undefined
      ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : {}),
  });
  if (!resp.ok) throw new Error(`API ${path}: ${resp.status}`);
  return resp.json() as Promise<T>;
}

export const api = {
  systemStatus: () => request<SystemStatus>("/system/status"),
  healthDeps: () => request<HealthDeps>("/health/deps"),
  wallets: () => request<{ items: WalletItem[]; count: number }>("/wallets"),
  signals: () => request<{ items: SignalItem[]; count: number }>("/signals"),
  positions: () => request<PositionsResponse>("/positions"),
  approvalQueue: () =>
    request<{ items: ApprovalItem[]; count: number }>("/approval-queue"),
  addWallet: (address: string, label?: string) =>
    request<{
      id: number;
      address: string;
      label: string | null;
      approval_state: string;
    }>("/wallets", "POST", { address, label: label || null }),
  walletAction: (walletId: number, action: "approve" | "reject" | "disable") =>
    request<{ wallet_id: number; approval_state: string }>(
      `/wallets/${walletId}/${action}`,
      "POST",
    ),
  runScoring: () =>
    request<{ scored: number; verdicts: Record<string, string> }>(
      "/scoring/run",
      "POST",
    ),
};

/** Poll an API call: immediate fetch, then every `intervalMs`. */
export function useApi<T>(
  fn: () => Promise<T>,
  intervalMs = 20000,
): { data: T | null; error: string | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    fn()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, [fn]);
  useEffect(() => {
    load();
    const t = setInterval(load, intervalMs);
    return () => clearInterval(t);
  }, [load, intervalMs]);
  return { data, error, refresh: load };
}

// --- Plain-language formatting helpers --------------------------------

export function fmtUsd(n: number): string {
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

export function fmtSignedUsd(n: number): string {
  return n >= 0 ? `+${fmtUsd(n)}` : fmtUsd(n);
}

/** A share price like 0.46 -> "46¢" (prediction markets quote in cents). */
export function fmtPrice(n: number): string {
  return `${(n * 100).toFixed(0)}¢`;
}

export function shortAddr(addr: string): string {
  return addr.length > 12 ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : addr;
}

export function walletName(w: {
  address: string;
  label: string | null;
}): string {
  return w.label ?? shortAddr(w.address);
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.floor(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.floor(hours)}h ago`;
  const days = hours / 24;
  if (days < 30) return `${Math.floor(days)}d ago`;
  return new Date(iso).toLocaleDateString();
}

/** What the bot did about a signal, in one short phrase. */
export function copyResult(s: SignalItem): {
  label: string;
  tone: "good" | "warn" | "muted" | "bad";
  detail: string;
} {
  if (s.status === "pending") {
    return {
      label: "Waiting",
      tone: "muted",
      detail:
        "In the review-delay window or paused by the kill switch. Nothing has happened yet — and nothing is lost.",
    };
  }
  const o = s.paper_order;
  if (!o) {
    return { label: "No copy attempt", tone: "muted", detail: "" };
  }
  if (o.status === "filled") {
    return {
      label: "Copied",
      tone: "good",
      detail:
        o.fill_price != null && o.filled_size != null
          ? `${o.filled_size.toFixed(1)} practice shares at ${fmtPrice(o.fill_price)}`
          : "Practice order filled",
    };
  }
  if (o.status === "partial") {
    return {
      label: "Partly copied",
      tone: "warn",
      detail:
        o.fill_price != null && o.filled_size != null
          ? `Only ${o.filled_size.toFixed(1)} shares available at ${fmtPrice(o.fill_price)}`
          : "Only part of the order could be filled",
    };
  }
  return {
    label: "Not copied",
    tone: "bad",
    detail: missReasonText(o.miss_reason),
  };
}

export function missReasonText(reason: string | null): string {
  switch (reason) {
    case "no_book_depth":
      return "No sellers available at a safe price right then.";
    case "market_closed":
      return "This market had already closed.";
    case "no_token_for_outcome":
      return "Couldn't identify the exact token to trade.";
    case "exposure_cap":
      return "Skipped on purpose to stay within your risk limits.";
    case "no_position_to_sell":
      return "Nothing to sell — you don't hold this position.";
    case "price_zone":
      return "Price was above the 90¢ safety limit, so it was skipped.";
    case "wallet_not_approved":
      return "This wallet was no longer approved when the trade ran.";
    default:
      return reason ? `Reason: ${reason}` : "Unknown reason.";
  }
}

export function approvalStateMeta(state: string): {
  label: string;
  className: string;
} {
  switch (state) {
    case "approved":
      return { label: "Following", className: "bg-green-900/60 text-green-300" };
    case "pending_review":
      return {
        label: "Needs your decision",
        className: "bg-amber-900/60 text-amber-300",
      };
    case "rejected":
      return { label: "Rejected", className: "bg-gray-800 text-gray-400" };
    case "disabled":
      return { label: "Paused", className: "bg-gray-800 text-gray-400" };
    default:
      return { label: "Discovered", className: "bg-gray-800 text-gray-400" };
  }
}
