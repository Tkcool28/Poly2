import PageShell, { EmptyState } from "../components/PageShell";

export default function Signals() {
  return (
    <PageShell title="Signals">
      <EmptyState message="No signals yet. The scoring engine and signal feed arrive in Chunk 2." />
    </PageShell>
  );
}
