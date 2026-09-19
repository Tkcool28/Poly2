# Source Identity Contract

> **STATUS: DRAFT — to be completed by the bounded Polymarket API audit,
> which is Chunk 2 task #1. No ingestion code ships before this contract is
> filled in and reviewed.**

## Why this document exists

Idempotency and deduplication depend on answering one question precisely:

> **What field or combination of fields uniquely identifies one trade, as
> reported by each Polymarket API we consume?**

Wrong assumptions here silently corrupt everything downstream: double-counted
trades inflate wallet P&L, missed duplicates break position accounting, and
bad keys make the copy bot fire twice on the same source trade.

## Known hazards (assumptions NOT allowed without evidence)

- `transactionHash` is a Polygon transaction hash. One transaction can
  contain MULTIPLE fills (batch orders, match settlements). A transaction
  hash alone must not be assumed to identify one trade.
- Different APIs (Data API, CLOB API, Subgraph/Goldsky) may assign different
  IDs to the same economic event, or none at all.
- Field stability across API versions is not guaranteed.

## Audit procedure (bounded — a day, not a week)

For each endpoint Chunk 2 will consume:

1. Fetch real samples (read-only, small page sizes, rate-limited).
2. Record the full field list of the trade/activity objects.
3. Look for collisions: find two distinct fills sharing a candidate key.
4. Determine the minimal stable unique key per endpoint.

Endpoints to audit:

- [ ] Data API `GET /trades` (and/or `GET /activity`)
- [ ] CLOB API trade/market endpoints
- [ ] Gamma API market metadata (condition_id ↔ clob_token_ids mapping)

## Contract (to be filled in)

| Endpoint | Canonical unique key | Notes / collision evidence |
|---|---|---|
| Data API trades | _TBD_ | _TBD_ |
| CLOB trades | _TBD_ | _TBD_ |

The value stored in `trades.polymarket_trade_id` is:

    _TBD — e.g. "{endpoint}:{canonical_key}" — decided by the audit._

## Cross-cutting identity rules (fixed now, not audit-dependent)

- Wallet addresses are stored lowercase (canonical EVM form).
- The tradable identity is the CLOB token ID (`trades.asset_id`, mapped via
  `markets.clob_token_ids`). Outcome text is display data, never a key.
- `markets.condition_id` is the market identity; the Gamma API provides the
  condition_id ↔ token IDs mapping.
