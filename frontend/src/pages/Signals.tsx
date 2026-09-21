import PageShell, { EmptyState, LoadError } from "../components/PageShell";
import {
  api,
  copyResult,
  fmtPrice,
  timeAgo,
  useApi,
  walletName,
} from "../lib/api";

const TONE_CLASSES: Record<string, string> = {
  good: "bg-green-900/60 text-green-300",
  warn: "bg-amber-900/60 text-amber-300",
  bad: "bg-red-900/60 text-red-300",
  muted: "bg-gray-800 text-gray-400",
};

/** Activity: every trade by a followed wallet, and what the bot did. */
export default function Signals() {
  const { data, error } = useApi(api.signals);
  const items = data?.items ?? [];

  return (
    <PageShell
      title="Activity"
      explainer="Every time a wallet you follow trades on Polymarket, it appears here — with what the bot did about it and why."
    >
      <LoadError error={error} />
      {items.length === 0 ? (
        <EmptyState
          title="No activity yet"
          message="Followed wallets haven't traded since you approved them. Trades from before approval are never copied — that's a safety rule."
        />
      ) : (
        <div className="space-y-3">
          {items.map((s) => {
            const result = copyResult(s);
            return (
              <div key={s.id} className="rounded-xl bg-gray-900 p-4">
                <div className="flex items-start justify-between gap-2">
                  <div className="text-sm font-medium leading-snug">
                    {s.market_question}
                  </div>
                  <span
                    className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${TONE_CLASSES[result.tone]}`}
                  >
                    {result.label}
                  </span>
                </div>
                <div className="mt-2 text-sm text-gray-300">
                  <span className="font-medium">
                    {walletName({
                      address: s.wallet_address,
                      label: s.wallet_label,
                    })}
                  </span>{" "}
                  {s.side === "BUY" ? "bought" : "sold"}{" "}
                  <span className="font-semibold">{s.outcome}</span> at{" "}
                  {fmtPrice(s.source_price)}
                </div>
                {result.detail && (
                  <div className="mt-1 text-sm text-gray-400">
                    {result.detail}
                  </div>
                )}
                <div className="mt-2 text-xs text-gray-500">
                  traded {timeAgo(s.t0_traded_at)} • spotted{" "}
                  {timeAgo(s.t1_detected_at)}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </PageShell>
  );
}
