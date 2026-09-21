import PageShell, { EmptyState, LoadError } from "../components/PageShell";
import {
  api,
  fmtPrice,
  fmtSignedUsd,
  fmtUsd,
  timeAgo,
  useApi,
  walletName,
} from "../lib/api";

/** Portfolio: what the bot currently holds (practice), and how it ended. */
export default function Portfolio() {
  const { data, error } = useApi(api.positions);
  const items = data?.items ?? [];
  const open = items.filter((i) => i.quantity > 0 && !i.settled_at);
  const settled = items.filter((i) => i.settled_at);
  const totals = data?.totals;

  return (
    <PageShell
      title="Portfolio"
      explainer="Your practice positions — shares the bot pretended to buy while copying. When a market resolves, winners pay $1 per share and losers pay $0."
    >
      <LoadError error={error} />

      <div className="grid grid-cols-2 gap-2">
        <div className="rounded-xl bg-gray-900 p-4 text-center">
          <div
            className={`text-2xl font-bold ${
              (totals?.realized_pnl_usd ?? 0) >= 0
                ? "text-green-400"
                : "text-red-400"
            }`}
          >
            {totals ? fmtSignedUsd(totals.realized_pnl_usd) : "…"}
          </div>
          <div className="mt-1 text-xs text-gray-400">
            realized practice P&L
          </div>
        </div>
        <div className="rounded-xl bg-gray-900 p-4 text-center">
          <div className="text-2xl font-bold">
            {totals ? fmtUsd(totals.open_cost_usd) : "…"}
          </div>
          <div className="mt-1 text-xs text-gray-400">
            practice money in open positions
          </div>
        </div>
      </div>

      <h3 className="mb-2 mt-6 text-sm font-semibold text-gray-300">
        Open positions ({open.length})
      </h3>
      {open.length === 0 ? (
        <EmptyState
          title="Nothing open"
          message="When the bot copies a buy, the position appears here until it's sold or the market resolves."
        />
      ) : (
        <div className="space-y-3">
          {open.map((p) => (
            <div key={p.id} className="rounded-xl bg-gray-900 p-4">
              <div className="text-sm font-medium leading-snug">
                {p.market_question}
              </div>
              <div className="mt-2 flex items-center justify-between text-sm">
                <span className="text-gray-300">
                  {p.quantity.toFixed(1)} shares of{" "}
                  <span className="font-semibold">{p.outcome}</span>
                </span>
                <span className="text-gray-400">
                  avg {fmtPrice(p.avg_price)}
                </span>
              </div>
              <div className="mt-1 text-xs text-gray-500">
                copied from{" "}
                {walletName({
                  address: p.wallet_address,
                  label: p.wallet_label,
                })}{" "}
                • cost {fmtUsd(p.quantity * p.avg_price)}
              </div>
            </div>
          ))}
        </div>
      )}

      <h3 className="mb-2 mt-6 text-sm font-semibold text-gray-300">
        Settled ({settled.length})
      </h3>
      {settled.length === 0 ? (
        <EmptyState
          title="No settled positions yet"
          message="When a market you hold resolves, the result is booked here — winners pay $1 per share, losers $0."
        />
      ) : (
        <div className="space-y-3">
          {settled.map((p) => (
            <div key={p.id} className="rounded-xl bg-gray-900 p-4">
              <div className="flex items-start justify-between gap-2">
                <div className="text-sm font-medium leading-snug">
                  {p.market_question}
                </div>
                <span
                  className={`shrink-0 text-sm font-bold ${
                    p.realized_pnl >= 0 ? "text-green-400" : "text-red-400"
                  }`}
                >
                  {fmtSignedUsd(p.realized_pnl)}
                </span>
              </div>
              <div className="mt-1 text-xs text-gray-500">
                {p.outcome} • settled {timeAgo(p.settled_at)}
              </div>
            </div>
          ))}
        </div>
      )}
    </PageShell>
  );
}
