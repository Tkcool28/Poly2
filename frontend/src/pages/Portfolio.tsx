import PageShell, { EmptyState } from "../components/PageShell";

export default function Portfolio() {
  return (
    <PageShell title="Portfolio">
      <EmptyState message="No positions. Paper trading starts in Chunk 2 — real money is never touched without an explicit live-trading gate." />
    </PageShell>
  );
}
