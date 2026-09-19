import { useEffect, useState } from "react";
import PageShell from "../components/PageShell";
import { api, type SystemStatus } from "../lib/api";

export default function Overview() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [wallets, setWallets] = useState<number | null>(null);
  const [signals, setSignals] = useState<number | null>(null);

  useEffect(() => {
    api.systemStatus().then(setStatus).catch(() => {});
    api.wallets().then((r) => setWallets(r.count)).catch(() => {});
    api.signals().then((r) => setSignals(r.count)).catch(() => {});
  }, []);

  const cards = [
    { label: "Tracked Wallets", value: wallets },
    { label: "Signals", value: signals },
    {
      label: "Mode",
      value: status ? (status.paper_mode ? "Paper" : "Live") : null,
    },
    {
      label: "Kill Switch",
      value: status ? (status.order_kill_switch ? "ON" : "OFF") : null,
    },
  ];

  return (
    <PageShell title="Overview">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {cards.map((c) => (
          <div key={c.label} className="rounded-lg bg-gray-900 p-4">
            <div className="text-sm text-gray-400">{c.label}</div>
            <div className="mt-1 text-2xl font-bold">
              {c.value === null ? "…" : c.value}
            </div>
          </div>
        ))}
      </div>
      <p className="mt-6 text-sm text-gray-500">
        Chunk 1 scaffolding: the stack is live, data pipelines are not.
        Wallets and signals will populate once ingestion lands in Chunk 2.
      </p>
    </PageShell>
  );
}
