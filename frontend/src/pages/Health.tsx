import { useEffect, useState } from "react";
import PageShell from "../components/PageShell";
import { api, type HealthDeps } from "../lib/api";

export default function Health() {
  const [deps, setDeps] = useState<HealthDeps | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .healthDeps()
      .then(setDeps)
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <PageShell title="System Health">
      {error && (
        <div className="rounded-lg bg-red-900/50 p-4 text-red-200">
          API unreachable: {error}
        </div>
      )}
      {deps && (
        <div className="space-y-2">
          <div className="text-sm text-gray-400">
            Overall:{" "}
            <span
              className={
                deps.status === "ok" ? "text-green-400" : "text-amber-400"
              }
            >
              {deps.status}
            </span>
          </div>
          {Object.entries(deps.checks).map(([name, state]) => (
            <div
              key={name}
              className="flex items-center justify-between rounded-lg bg-gray-900 px-4 py-3"
            >
              <span className="font-medium">{name}</span>
              <span
                className={state === "ok" ? "text-green-400" : "text-red-400"}
              >
                {state}
              </span>
            </div>
          ))}
        </div>
      )}
    </PageShell>
  );
}
