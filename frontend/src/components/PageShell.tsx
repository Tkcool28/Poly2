import type { ReactNode } from "react";

export default function PageShell({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div>
      <h2 className="mb-4 text-2xl font-semibold">{title}</h2>
      {children}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-dashed border-gray-700 p-8 text-center text-gray-400">
      {message}
    </div>
  );
}
