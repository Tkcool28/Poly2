import { useState } from "react";
import { Link } from "react-router-dom";
import PageShell, { EmptyState, LoadError } from "../components/PageShell";
import {
  api,
  approvalStateMeta,
  shortAddr,
  timeAgo,
  useApi,
} from "../lib/api";

/** Wallets: everyone the bot tracks, their state, and their score. */
export default function Wallets() {
  const { data, error, refresh } = useApi(api.wallets);
  const [actionError, setActionError] = useState<string | null>(null);
  const items = data?.items ?? [];

  async function disable(id: number, address: string) {
    if (
      !window.confirm(
        `Stop following ${shortAddr(address)}? The bot will no longer copy their trades. You can't undo this from the app.`,
      )
    ) {
      return;
    }
    setActionError(null);
    try {
      await api.walletAction(id, "disable");
      refresh();
    } catch (e) {
      setActionError(`Couldn't stop following that wallet. (${e})`);
    }
  }

  return (
    <PageShell
      title="Wallets"
      explainer="Polymarket wallets the bot knows about. Only wallets marked “Following” get copied — and only trades they make after you approved them."
    >
      <LoadError error={error} />
      {actionError && (
        <div className="mb-3 rounded-lg bg-red-900/50 p-3 text-sm text-red-200">
          {actionError}
        </div>
      )}
      {items.length === 0 ? (
        <EmptyState
          title="No wallets yet"
          message="Candidate wallets are added by the operator (you) for now. Once scored, promising ones appear in the Review tab."
        />
      ) : (
        <div className="space-y-3">
          {items.map((w) => {
            const meta = approvalStateMeta(w.approval_state);
            return (
              <div key={w.id} className="rounded-xl bg-gray-900 p-4">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate font-medium">
                      {w.label ?? shortAddr(w.address)}
                    </div>
                    {w.label && (
                      <div className="text-xs text-gray-500">
                        {shortAddr(w.address)}
                      </div>
                    )}
                  </div>
                  <span
                    className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${meta.className}`}
                  >
                    {meta.label}
                  </span>
                </div>
                <div className="mt-2 flex items-center justify-between text-xs text-gray-500">
                  <span>
                    {w.composite_score != null
                      ? `score ${w.composite_score.toFixed(0)}/100`
                      : "not scored yet"}
                    {w.approved_at &&
                      ` • following since ${timeAgo(w.approved_at)}`}
                  </span>
                  {w.approval_state === "pending_review" && (
                    <Link to="/approvals" className="text-amber-300">
                      Decide ›
                    </Link>
                  )}
                </div>
                {w.approval_state === "approved" && (
                  <button
                    onClick={() => disable(w.id, w.address)}
                    className="mt-3 min-h-[44px] w-full rounded-lg border border-gray-700 text-sm text-gray-300 active:bg-gray-800"
                  >
                    Stop following
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </PageShell>
  );
}
