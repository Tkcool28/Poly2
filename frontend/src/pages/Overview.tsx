import { Link } from "react-router-dom";
import PageShell, { EmptyState, LoadError } from "../components/PageShell";
import {
  api,
  copyResult,
  fmtSignedUsd,
  fmtUsd,
  timeAgo,
  useApi,
  walletName,
} from "../lib/api";

/** Home: the whole system on one phone screen, in plain language. */
export default function Overview() {
  const positions = useApi(api.positions);
  const signals = useApi(api.signals);
  const wallets = useApi(api.wallets);
  const queue = useApi(api.approvalQueue, 30000);
  const health = useApi(api.healthDeps, 60000);

  const totals = positions.data?.totals;
  const following =
    wallets.data?.items.filter((w) => w.approval_state === "approved")
      .length ?? 0;
  const recent = signals.data?.items.slice(0, 3) ?? [];
  const pending = queue.data?.count ?? 0;

  return (
    <PageShell title="Home">
      <LoadError
        error={positions.error ?? signals.error ?? wallets.error ?? null}
      />

      {/* Practice P&L hero */}
      <div className="rounded-xl bg-gray-900 p-5 text-center">
        <div className="text-sm text-gray-400">
          Practice profit / loss so far
        </div>
        <div
          className={`mt-1 text-4xl font-bold ${
            (totals?.realized_pnl_usd ?? 0) >= 0
              ? "text-green-400"
              : "text-red-400"
          }`}
        >
          {totals ? fmtSignedUsd(totals.realized_pnl_usd) : "…"}
        </div>
        <div className="mt-2 text-xs text-gray-500">
          Not real money — this is what copying would have made or lost.
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-center">
        <div className="rounded-xl bg-gray-900 p-3">
          <div className="text-xl font-bold">{following}</div>
          <div className="text-xs text-gray-400">wallets followed</div>
        </div>
        <div className="rounded-xl bg-gray-900 p-3">
          <div className="text-xl font-bold">{totals?.open_count ?? "…"}</div>
          <div className="text-xs text-gray-400">open positions</div>
        </div>
        <div className="rounded-xl bg-gray-900 p-3">
          <div className="text-xl font-bold">
            {totals ? fmtUsd(totals.open_cost_usd) : "…"}
          </div>
          <div className="text-xs text-gray-400">practice money in play</div>
        </div>
      </div>

      {/* Needs your decision */}
      {pending > 0 && (
        <Link
          to="/approvals"
          className="mt-3 flex items-center justify-between rounded-xl bg-amber-900/40 p-4"
        >
          <div>
            <div className="font-semibold text-amber-200">
              {pending} wallet{pending > 1 ? "s" : ""} waiting for your
              decision
            </div>
            <div className="text-xs text-amber-200/70">
              Nothing gets copied until you approve a wallet.
            </div>
          </div>
          <span className="text-2xl text-amber-200">›</span>
        </Link>
      )}

      {/* Recent activity */}
      <h3 className="mb-2 mt-6 text-sm font-semibold text-gray-300">
        Latest activity
      </h3>
      {recent.length === 0 ? (
        <EmptyState
          title="Nothing yet"
          message="When a wallet you follow trades on Polymarket, it shows up here — along with whether the bot copied it."
        />
      ) : (
        <div className="space-y-2">
          {recent.map((s) => {
            const result = copyResult(s);
            return (
              <div key={s.id} className="rounded-xl bg-gray-900 p-3">
                <div className="text-sm">
                  <span className="font-medium">
                    {walletName({ address: s.wallet_address, label: s.wallet_label })}
                  </span>{" "}
                  {s.side === "BUY" ? "bought" : "sold"}{" "}
                  <span className="font-medium">{s.outcome}</span> on “
                  {s.market_question}”
                </div>
                <div className="mt-1 flex items-center justify-between text-xs">
                  <span
                    className={
                      result.tone === "good"
                        ? "text-green-400"
                        : result.tone === "bad"
                          ? "text-red-400"
                          : result.tone === "warn"
                            ? "text-amber-400"
                            : "text-gray-400"
                    }
                  >
                    {result.label}
                    {result.detail ? ` — ${result.detail}` : ""}
                  </span>
                  <span className="text-gray-500">
                    {timeAgo(s.t1_detected_at)}
                  </span>
                </div>
              </div>
            );
          })}
          <Link
            to="/signals"
            className="block rounded-xl border border-gray-800 p-3 text-center text-sm text-gray-300"
          >
            See all activity
          </Link>
        </div>
      )}

      {/* System status */}
      <h3 className="mb-2 mt-6 text-sm font-semibold text-gray-300">System</h3>
      <div className="rounded-xl bg-gray-900 p-3 text-sm">
        {health.data ? (
          <div className="flex items-center justify-between">
            <span className="text-gray-400">Bot & database</span>
            <span
              className={
                health.data.status === "ok" ? "text-green-400" : "text-amber-400"
              }
            >
              {health.data.status === "ok"
                ? "All good"
                : `Check needed: ${Object.entries(health.data.checks)
                    .filter(([, v]) => v !== "ok")
                    .map(([k]) => k)
                    .join(", ")}`}
            </span>
          </div>
        ) : (
          <span className="text-gray-500">Checking…</span>
        )}
      </div>
    </PageShell>
  );
}
