import PageShell, { EmptyState } from "../components/PageShell";

export default function Approvals() {
  return (
    <PageShell title="Approval Queue">
      <EmptyState message="Nothing awaiting review. Wallets that graduate from scoring will appear here for human approval in Chunk 2." />
    </PageShell>
  );
}
