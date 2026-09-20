# Wallet Accounting Method (PR-C)

> **Why this doc exists:** six months from now, when a wallet's P&L number
> looks wrong, this is the page that explains exactly how it was computed
> and what was deliberately NOT trusted.

## The method: pure cash-flow

For each wallet × market, using only ingested trades:

| Event | Cash flow |
|---|---|
| BUY `s` shares @ `p` | `−s × p` |
| SELL `s` shares @ `p` | `+s × p` |
| Market resolves, winning shares held | `+1 per share` |

**Realized P&L (resolved market) = total cash flows + payout.**

Deliberate choices:

* **Only resolved markets produce P&L.** Open positions are reported as
  share counts, never as value. Guessing unrealized value is how
  accounting quietly lies.
* **All money math is `Decimal`.** Floats never touch a total
  (`0.1 + 0.2` drift is a tested non-issue here).
* **Selling before resolution just locks cash** — no "cost basis"
  allocation needed. This is why cash-flow beats weighted-average-cost
  for a copy-trading audit trail: every number traces to actual money.

## Settlement detection

Source: Gamma market objects. A market is settled when `closed=true` AND
`outcomePrices` collapses to exactly one `1` (e.g. outcomes `["Up","Down"]`,
prices `["1","0"]` → "Up" won).

**Fail-closed:** anything ambiguous — mid prices, two `1`s, no `1`,
garbage JSON, length mismatch — is skipped and logged, NEVER settled.
A wrong settlement is worse than a late one.

⚠ **Pending probe verification:** the outcomePrices-collapse behavior is
standard Polymarket behavior but has not yet been verified against a
freshly resolved market from an unrestricted network. Tracked in
`docs/source-identity-contract.md`. The strict parser means the worst
case is "we don't settle yet," never "we settle wrong."

## Validation before scoring

`reconcile_wallet` compares our realized P&L against the Data API
`/positions` `realizedPnl` total (probe-verified field, PR-B). It is a
**report, not a gate**: the API covers the wallet's full history while we
only count ingested trades, so deltas during backfill are expected and
logged — never raised as errors.

Scoring (PR-D) consumes `compute_wallet_accounting().summary` — nothing
else writes those numbers.
