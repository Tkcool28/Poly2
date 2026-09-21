import { api, useApi } from "../lib/api";

/**
 * Always-on banner answering the two questions that matter most:
 * "is real money involved?" and "is the bot allowed to trade right now?"
 * Written in plain language — no jargon.
 */
export default function SafetyBanner() {
  const { data: status, error } = useApi(api.systemStatus, 30000);

  if (error) {
    return (
      <div className="bg-red-900 px-4 py-2 text-center text-sm font-semibold">
        Can't reach the bot — assume nothing is running.
      </div>
    );
  }
  if (!status) return null;

  if (!status.paper_mode) {
    return (
      <div className="bg-red-700 px-4 py-2 text-center text-sm font-semibold">
        LIVE MODE — real money is at stake
      </div>
    );
  }

  return (
    <div className="bg-emerald-800 px-4 py-2 text-center text-sm">
      <span className="font-semibold">Practice mode</span> — no real money
      {status.order_kill_switch && (
        <span className="ml-1 text-amber-300">
          • copying is paused (kill switch on)
        </span>
      )}
    </div>
  );
}
