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

async function get<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}${path}`);
  if (!resp.ok) throw new Error(`API ${path}: ${resp.status}`);
  return resp.json() as Promise<T>;
}

export const api = {
  systemStatus: () => get<SystemStatus>("/system/status"),
  healthDeps: () => get<HealthDeps>("/health/deps"),
  wallets: () => get<{ items: unknown[]; count: number }>("/wallets"),
  signals: () => get<{ items: unknown[]; count: number }>("/signals"),
};
