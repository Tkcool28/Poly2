import { useState } from "react";
import PageShell, { EmptyState, LoadError } from "../components/PageShell";
import { api, shortAddr, timeAgo, useApi } from "../lib/api";

/**
 * Review: the human gate. No wallet is ever copied until a person taps
 * Approve here. Big thumb-sized buttons, confirm before acting.
 */
export default function Approvals() {
  const { data, error, refresh } = useApi(api.approvalQueue);
  const [busy, setBusy] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [scoringMsg, setScoringMsg] = useState<string | null>(null);
  const items = data?.items ?? [];

  async function decide(
    walletId: number,
    action: "approve" | "reject",
    address: string,
  ) {
    const verb = action === "approve" ? "Follow" : "Reject";
    if (
      !window.confirm(
        action === "approve"
          ? `${verb} ${shortAddr(address)}? The bot will start practice-copying their new trades.`
          : `${verb} ${shortAddr(address)}? The bot will ignore this wallet.`,
      )
    ) {
      return;
    }
    setBusy(walletId);
    setActionError(null);
    try {
      await api.walletAction(walletId, action);
    } catch (e) {
      setActionError(
        `That didn't work — the wallet's state may have just changed. (${e})`,
      );
    }
    setBusy(null);
    refresh();
  }

  async function runScoring() {
    setScoringMsg("Checking…");
    setActionError(null);
    try {
      const r = await api.runScoring();
      const queued = Object.values(r.verdicts).filter(
        (v) => v === "queued_for_review",
      ).length;
      setScoringMsg(
        r.scored === 0
          ? "No new wallets to check right now."
          : `Checked ${r.scored} wallet${r.scored > 1 ? "s" : ""}: ${queued} sent to Review.`,
      );
    } catch (e) {
      setScoringMsg(null);
      setActionError(`Scoring failed. (${e})`);
    }
    refresh();
  }

  return (
    <PageShell
      title="Review"
      explainer="Wallets the scorer thinks are worth copying. Nothing happens until you decide — approving means the bot starts practice-copying their new trades."
    >
      <LoadError error={error} />
      {actionError && (
        <div className="mb-3 rounded-lg bg-red-900/50 p-3 text-sm text-red-200">
          {actionError}
        </div>
      )}

      <button
        onClick={runScoring}
        className="mb-4 min-h-[48px] w-full rounded-xl border border-gray-700 text-sm font-medium text-gray-200 active:bg-gray-800"
      >
        Check for new candidates
      </button>
      {scoringMsg && (
        <div className="mb-4 text-center text-xs text-gray-400">
          {scoringMsg}
        </div>
      )}

      {items.length === 0 ? (
        <EmptyState
          title="All caught up"
          message="No wallets are waiting for a decision. New candidates appear here after scoring finds a wallet worth a look."
        />
      ) : (
        <div className="space-y-3">
          {items.map((item) => (
            <div key={item.entry_id} className="rounded-xl bg-gray-900 p-4">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-medium">
                    {item.label ?? shortAddr(item.address)}
                  </div>
                  <div className="text-xs text-gray-500">
                    {shortAddr(item.address)} • flagged{" "}
                    {timeAgo(item.queued_at)}
                  </div>
                </div>
                {item.score?.composite_score != null && (
                  <div className="shrink-0 text-right">
                    <div className="text-xl font-bold text-amber-300">
                      {item.score.composite_score.toFixed(0)}
                    </div>
                    <div className="text-[10px] text-gray-500">
                      score / 100
                    </div>
                  </div>
                )}
              </div>
              {item.score && (
                <div className="mt-2 text-xs text-gray-400">
                  Based on the last {item.score.window_days} days of their
                  trading
                  {item.score.profit_factor != null &&
                    ` • made $${item.score.profit_factor.toFixed(2)} for every $1 lost`}
                </div>
              )}
              <div className="mt-4 grid grid-cols-2 gap-3">
                <button
                  disabled={busy === item.wallet_id}
                  onClick={() =>
                    decide(item.wallet_id, "reject", item.address)
                  }
                  className="min-h-[48px] rounded-xl border border-gray-700 font-medium text-gray-300 active:bg-gray-800 disabled:opacity-40"
                >
                  Reject
                </button>
                <button
                  disabled={busy === item.wallet_id}
                  onClick={() =>
                    decide(item.wallet_id, "approve", item.address)
                  }
                  className="min-h-[48px] rounded-xl bg-green-700 font-semibold text-white active:bg-green-600 disabled:opacity-40"
                >
                  {busy === item.wallet_id ? "Working…" : "Follow"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </PageShell>
  );
}
