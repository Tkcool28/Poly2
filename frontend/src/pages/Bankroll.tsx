import { useState } from "react";
import PageShell, { EmptyState, LoadError } from "../components/PageShell";
import {
  api,
  fmtSignedUsd,
  fmtUsd,
  timeAgo,
  useApi,
  type BankrollLedgerItem,
} from "../lib/api";

function ledgerLabel(item: BankrollLedgerItem): string {
  if (item.entry_type === "profit_withdrawal") {
    return "Withdrawn to your account";
  }
  const kind = item.context?.kind;
  if (kind === "settlement") return "Market settled";
  if (kind === "paper_sell") return "Copied sell";
  return "Realized P&L";
}

/** Bankroll: the practice stake, profit sweeps, and the account statement. */
export default function Bankroll() {
  const { data, error, refresh } = useApi(api.bankroll, 20000);
  const ledger = useApi(api.bankrollLedger, 20000);
  const [limit, setLimit] = useState("");
  const [floor, setFloor] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const body: { profit_limit_usd?: number; stop_loss_floor_usd?: number } = {};
      if (limit.trim() !== "") body.profit_limit_usd = Number(limit);
      if (floor.trim() !== "") body.stop_loss_floor_usd = Number(floor);
      await api.updateBankrollSettings(body);
      setSaved(true);
      setLimit("");
      setFloor("");
      refresh();
      ledger.refresh();
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const balanceTone =
    data && data.bankroll_balance_usd < data.starting_bankroll_usd
      ? "text-red-400"
      : "text-green-400";

  return (
    <PageShell
      title="Bankroll"
      explainer="The practice stake the bot trades with. Profit above your limit is swept out like a real withdrawal — and there's no refill: a loss is a loss."
    >
      <LoadError error={error ?? ledger.error ?? null} />

      {/* Balance hero */}
      <div className="rounded-xl bg-gray-900 p-5 text-center">
        <div className="text-sm text-gray-400">Practice bankroll</div>
        <div className={`mt-1 text-4xl font-bold ${balanceTone}`}>
          {data ? fmtUsd(data.bankroll_balance_usd) : "…"}
        </div>
        <div className="mt-2 text-xs text-gray-500">
          Started with {data ? fmtUsd(data.starting_bankroll_usd) : "…"} — not
          real money, but the bot treats it like it is.
        </div>
      </div>

      {data?.stop_loss_hit && (
        <div className="mt-3 rounded-xl bg-red-900/40 p-4 text-sm text-red-200">
          Stop-loss floor reached ({fmtUsd(data.stop_loss_floor_usd ?? 0)}). The
          bot won't open new positions while the balance stays this low.
          Positions it already holds still sell and settle normally.
        </div>
      )}

      <div className="mt-3 grid grid-cols-2 gap-2 text-center">
        <div className="rounded-xl bg-gray-900 p-3">
          <div className="text-xl font-bold">
            {data ? fmtUsd(data.available_cash_usd) : "…"}
          </div>
          <div className="text-xs text-gray-400">free cash</div>
        </div>
        <div className="rounded-xl bg-gray-900 p-3">
          <div className="text-xl font-bold">
            {data ? fmtUsd(data.open_cost_usd) : "…"}
          </div>
          <div className="text-xs text-gray-400">tied up in open positions</div>
        </div>
        <div className="rounded-xl bg-gray-900 p-3">
          <div
            className={`text-xl font-bold ${
              (data?.realized_pnl_total_usd ?? 0) >= 0
                ? "text-green-400"
                : "text-red-400"
            }`}
          >
            {data ? fmtSignedUsd(data.realized_pnl_total_usd) : "…"}
          </div>
          <div className="text-xs text-gray-400">lifetime realized P&L</div>
        </div>
        <div className="rounded-xl bg-gray-900 p-3">
          <div className="text-xl font-bold text-emerald-400">
            {data ? fmtUsd(data.withdrawn_total_usd) : "…"}
          </div>
          <div className="text-xs text-gray-400">
            withdrawn to “your account”
          </div>
        </div>
      </div>

      {/* Settings */}
      <h3 className="mb-2 mt-6 text-sm font-semibold text-gray-300">
        Profit & safety limits
      </h3>
      <div className="rounded-xl bg-gray-900 p-4">
        <div className="text-xs text-gray-400">
          Un-withdrawn profit{" "}
          <span className="font-semibold text-gray-200">
            {data ? fmtUsd(data.unwithdrawn_profit_usd) : "…"}
          </span>
          {data?.profit_limit_usd ? (
            <>
              {" "}
              — hits the limit at {fmtUsd(data.profit_limit_usd)} and is swept
              out automatically.
            </>
          ) : (
            <> — profit sweeps are off.</>
          )}
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <label className="block">
            <span className="text-xs text-gray-400">
              Profit limit ($, 0 = off)
            </span>
            <input
              type="number"
              min="0"
              step="1"
              value={limit}
              placeholder={
                data?.profit_limit_usd != null
                  ? String(data.profit_limit_usd)
                  : "off"
              }
              onChange={(e) => setLimit(e.target.value)}
              className="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-xs text-gray-400">
              Stop-loss floor ($, 0 = off)
            </span>
            <input
              type="number"
              min="0"
              step="1"
              value={floor}
              placeholder={
                data?.stop_loss_floor_usd != null
                  ? String(data.stop_loss_floor_usd)
                  : "off"
              }
              onChange={(e) => setFloor(e.target.value)}
              className="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm"
            />
          </label>
        </div>
        <button
          onClick={save}
          disabled={saving || (limit.trim() === "" && floor.trim() === "")}
          className="mt-3 w-full rounded-lg bg-indigo-600 py-2 text-sm font-semibold disabled:opacity-40"
        >
          {saving ? "Saving…" : "Save limits"}
        </button>
        {saved && (
          <div className="mt-2 text-xs text-green-400">Limits saved.</div>
        )}
        {saveError && (
          <div className="mt-2 text-xs text-red-400">Save failed: {saveError}</div>
        )}
      </div>

      {/* Ledger */}
      <h3 className="mb-2 mt-6 text-sm font-semibold text-gray-300">
        Account statement
      </h3>
      {ledger.data && ledger.data.items.length === 0 ? (
        <EmptyState
          title="No movements yet"
          message="Every copied sell, settlement, and profit withdrawal lands here as a ledger entry."
        />
      ) : (
        <div className="space-y-2">
          {(ledger.data?.items ?? []).map((item) => (
            <div key={item.id} className="rounded-xl bg-gray-900 p-3 text-sm">
              <div className="flex items-center justify-between">
                <span className="text-gray-300">{ledgerLabel(item)}</span>
                <span
                  className={`font-bold ${
                    item.amount >= 0 ? "text-green-400" : "text-red-400"
                  }`}
                >
                  {fmtSignedUsd(item.amount)}
                </span>
              </div>
              <div className="mt-1 flex items-center justify-between text-xs text-gray-500">
                <span>balance {fmtUsd(item.balance_after)}</span>
                <span>{timeAgo(item.created_at)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </PageShell>
  );
}
