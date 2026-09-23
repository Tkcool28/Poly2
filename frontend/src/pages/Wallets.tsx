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

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

/** Wallets: everyone the bot tracks, their state, and their score. */
export default function Wallets() {
  const { data, error, refresh } = useApi(api.wallets);
  const [actionError, setActionError] = useState<string | null>(null);
  const [address, setAddress] = useState("");
  const [label, setLabel] = useState("");
  const [adding, setAdding] = useState(false);
  const [addMsg, setAddMsg] = useState<{
    tone: "good" | "bad";
    text: string;
  } | null>(null);
  const items = data?.items ?? [];

  async function add(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = address.trim();
    if (!ADDRESS_RE.test(trimmed)) {
      setAddMsg({
        tone: "bad",
        text: "That doesn't look like a wallet address — it should be 0x followed by 40 letters/numbers.",
      });
      return;
    }
    setAdding(true);
    setAddMsg(null);
    setActionError(null);
    try {
      const w = await api.addWallet(trimmed, label.trim() || undefined);
      setAddress("");
      setLabel("");
      setAddMsg({
        tone: "good",
        text: `Added ${shortAddr(w.address)}. Next: give the bot a poll cycle or two to pull their recent trades, then tap “Check for new candidates” in the Review tab to score them — you decide whether to follow.`,
      });
      refresh();
    } catch (e) {
      const status = String(e).includes("409")
        ? "That wallet is already being tracked."
        : `Couldn't add it. (${e})`;
      setAddMsg({ tone: "bad", text: status });
    }
    setAdding(false);
  }

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

      {/* Manual intake — Chunk 2 has no automated discovery */}
      <form
        onSubmit={add}
        className="mb-5 rounded-xl bg-gray-900 p-4"
      >
        <div className="font-medium">Add a wallet to track</div>
        <p className="mt-1 text-xs text-gray-500">
          Paste a Polymarket wallet address (starts with 0x). The bot starts
          watching it for scoring — nothing gets copied until you approve it.
        </p>
        <input
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder="0x…"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          className="mt-3 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-3 text-sm outline-none focus:border-gray-500"
        />
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Label (optional, e.g. “Sharp bettor”)"
          maxLength={120}
          className="mt-2 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-3 text-sm outline-none focus:border-gray-500"
        />
        <button
          type="submit"
          disabled={adding}
          className="mt-3 min-h-[48px] w-full rounded-xl bg-blue-700 font-semibold text-white active:bg-blue-600 disabled:opacity-40"
        >
          {adding ? "Adding…" : "Add wallet"}
        </button>
        {addMsg && (
          <div
            className={`mt-3 rounded-lg p-3 text-sm ${
              addMsg.tone === "good"
                ? "bg-green-900/50 text-green-200"
                : "bg-red-900/50 text-red-200"
            }`}
          >
            {addMsg.text}
          </div>
        )}
      </form>
      {items.length === 0 ? (
        <EmptyState
          title="No wallets yet"
          message="Add a wallet above — the bot will start tracking it for scoring, and promising candidates land in the Review tab."
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
                    {w.approval_state === "pending_review"
                      ? "Pending human review"
                      : w.composite_score != null
                        ? `score ${w.composite_score.toFixed(0)}/100`
                        : w.score_verdict === "insufficient_history"
                          ? "Insufficient history"
                          : w.score_verdict === "score_rejected"
                            ? "Scored — did not qualify"
                            : w.score_computed_at
                              ? "Scored — no numeric score"
                              : "Not scored yet"}
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
