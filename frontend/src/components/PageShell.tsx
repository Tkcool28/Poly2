import type { ReactNode } from "react";

export default function PageShell({
  title,
  explainer,
  children,
}: {
  title: string;
  /** One plain-language sentence explaining what this page shows. */
  explainer?: string;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-lg">
      <h2 className="text-xl font-semibold">{title}</h2>
      {explainer && (
        <p className="mb-4 mt-1 text-sm text-gray-400">{explainer}</p>
      )}
      {!explainer && <div className="mb-4" />}
      {children}
    </div>
  );
}

export function EmptyState({
  title,
  message,
}: {
  title: string;
  message: string;
}) {
  return (
    <div className="rounded-xl border border-dashed border-gray-700 p-6 text-center">
      <div className="font-medium text-gray-300">{title}</div>
      <p className="mt-1 text-sm text-gray-500">{message}</p>
    </div>
  );
}

export function LoadError({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="mb-3 rounded-lg bg-red-900/50 p-3 text-sm text-red-200">
      Couldn't load the latest data. Showing what we have. ({error})
    </div>
  );
}
