import PageShell, { EmptyState } from "../components/PageShell";

export default function Wallets() {
  return (
    <PageShell title="Wallets">
      <EmptyState message="No wallets tracked yet. Wallet discovery and scoring arrive in Chunk 2." />
    </PageShell>
  );
}
