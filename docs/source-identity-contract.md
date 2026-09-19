# Source Identity Contract

> **STATUS: FULLY VERIFIED ✅ — probe run via GitHub Actions
> (`probe_source_identity.py`) on 2026-09-19 returned RESULT: PASS for all
> four checks: Data API /trades identity (200-trade sample, 0 composite
> collisions), Data API /positions, Gamma /markets, CLOB
> /markets/{condition_id}. No ⚠ PENDING-VPS items remain.**

## Why this document exists

Idempotency and deduplication depend on answering one question precisely:

> **What field or combination of fields uniquely identifies one trade, as
> reported by each Polymarket API we consume?**

## Audit findings — Data API `GET https://data-api.polymarket.com/trades`

Sampled live responses (2026-09-19). Observed fields per trade object:

| Field | Type | Notes |
|---|---|---|
| `proxyWallet` | address | The trading wallet (Polymarket proxy). Our wallet identity. |
| `side` | BUY / SELL | |
| `asset` | decimal string | **The CLOB token ID.** Tradable identity. Maps to `trades.asset_id`. |
| `conditionId` | hex string | Market identity. Maps to `markets.condition_id`. |
| `size` | float | Share amount. |
| `price` | float | |
| `timestamp` | unix **seconds** | ⚠ Second granularity — NOT unique per trade. |
| `transactionHash` | hex string | Polygon tx. ⚠ One tx can contain multiple fills. |
| `title`, `slug`, `eventSlug`, `icon` | strings | Display data. |
| `outcome` | string | Display data. |
| `outcomeIndex` | int | ⚠ UNRELIABLE: observed both `0`/`1` and `999` for identical fields. Do not use as a key. |
| `name`, `pseudonym`, `bio`, `profileImage*` | strings | Profile metadata. |

**There is no trade-level unique ID field.** Identity must be a composite.

### Observed collision evidence (real, from the 2026-09-19 sample)

Wallet `0xc69bd5...c076` (`vtcchampion52`) placed multiple DISTINCT trades of
identical size (5), price (0.002), and asset, within the SAME second
(1789795243–1789795246), with different transaction hashes.

Consequences:
- `transactionHash` alone is insufficient (one tx may carry multiple fills).
- `(wallet, asset, size, price, timestamp)` alone is ALSO insufficient —
  distinct trades can share all five fields in the same second.

### Canonical trade key (DECIDED)

```
polymarket_trade_id =
    "data-api:" + transactionHash + ":" + proxyWallet + ":" + asset
             + ":" + size + ":" + price + ":" + timestamp
```

- Residual risk: two identical fills of the same size/price for the same
  wallet in one transaction would collapse into one row (undercount). Judged
  rare; mitigated by periodic position reconciliation against
  Data API `GET /positions?user=<wallet>` — ✅ probe-verified: the endpoint
  returns rich fields including `realizedPnl`, `cashPnl`, `avgPrice`,
  `totalBought`, `negativeRisk`, `redeemable`, `entryFeesUsdc`.
- `trades.polymarket_trade_id` widened to String(160) in migration 0002 to
  fit this composite.

## Market / token identity

- Market: `conditionId` (hex, `markets.condition_id`).
- Tradable unit: CLOB token ID (`asset` above → `trades.asset_id`).
- The condition_id ↔ {outcome: clob_token_id} mapping populates
  `markets.clob_token_ids` from Gamma API market objects (`clobTokenIds`
  field). ✅ probe-verified, with an important quirk: **Gamma returns
  `clobTokenIds` and `outcomes` as JSON-ENCODED STRINGS** (e.g.
  `"[\"4667...\", \"8761...\"]"` and `"[\"Up\", \"Down\"]"`), not native
  arrays — parsers MUST `json.loads` them (handled by
  `PolymarketClient._parse_json_string`). CLOB API
  `/markets/{condition_id}` token_ids match Gamma exactly (verified).

## Wallet identity

- `proxyWallet`, stored lowercase. Users trade through Polymarket proxy
  wallets; this is the correct observable identity.

## Endpoint audit checklist

| Endpoint | Purpose | Status |
|---|---|---|
| Data API `GET /trades` | Trade ingestion | ✅ Audited 2026-09-19; probe PASS (200 sample, 0 collisions) |
| Data API `GET /positions` | Position reconciliation | ✅ Probe PASS 2026-09-19 |
| Gamma API `GET /markets` | Market metadata + token mapping | ✅ Probe PASS 2026-09-19 (clobTokenIds = JSON string) |
| CLOB API `GET /markets/{condition_id}` | Token mapping fallback | ✅ Probe PASS 2026-09-19 (token_ids match Gamma) |
| Data API `GET /activity` | Redemptions/liquidity events for accounting | ⏳ Not yet audited — scheduled for PR-C (settlement feed) |

## Fixed rules (not audit-dependent)

- Wallet addresses stored lowercase.
- Tradable identity is the CLOB token ID; outcome text is display data.
- `outcomeIndex` is never used as a key.
- Gamma `clobTokenIds` / `outcomes` are JSON-encoded strings — always parse.
- Anything unverifiable from the sandbox is verified by
  `backend/scripts/probe_source_identity.py` (GitHub Actions,
  workflow_dispatch) before the code that depends on it ships.
