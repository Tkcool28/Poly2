import { useEffect, useState } from "react";
import { api, type SystemStatus } from "../lib/api";

/** Always-on safety banner: paper mode + kill switch state, straight from the API. */
export default function SafetyBanner() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    api
      .systemStatus()
      .then(setStatus)
      .catch(() => setError(true));
  }, []);

  if (error) {
    return (
      <div className="bg-red-900 px-4 py-2 text-center text-sm font-semibold">
        API UNREACHABLE — status unknown. Assume nothing is running.
      </div>
    );
  }
  if (!status) return null;

  const parts: string[] = [];
  if (status.paper_mode) parts.push("PAPER MODE");
  else parts.push("LIVE MODE");
  if (status.order_kill_switch) parts.push("KILL SWITCH ON");
  parts.push(status.environment.toUpperCase());

  const safe = status.paper_mode && status.order_kill_switch;
  return (
    <div
      className={`px-4 py-2 text-center text-sm font-semibold ${
        safe ? "bg-amber-700" : "bg-red-700"
      }`}
    >
      {parts.join("  •  ")}
    </div>
  );
}
