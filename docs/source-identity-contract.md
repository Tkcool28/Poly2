# Source Identity Contract

> **STATUS: AUDITED against live Data API (2026-09-19), with items marked
> ⚠ PENDING-VPS requiring verification from an unrestricted network (the
> build sandbox cannot reach Gamma/CLOB APIs). Run
> `backend/scripts/probe_source_identity.py` on the VPS to close them.**

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
  Data API `GET /positions?user=<wallet>` (⚠ PENDING-VPS verification).
- `trades.polymarket_trade_id` widened to String(160) in migration 0002 to
  fit this composite.

## Market / token identity

- Market: `conditionId` (hex, `markets.condition_id`).
- Tradable unit: CLOB token ID (`asset` above → `trades.asset_id`).
- The condition_id ↔ {outcome: clob_token_id} mapping populates
  `markets.clob_token_ids` from Gamma API market objects (`clobTokenIds`
  field). ⚠ PENDING-VPS: Gamma API unreachable from the build sandbox;
  confirm field name and structure via the probe script.

## Wallet identity

- `proxyWallet`, stored lowercase. Users trade through Polymarket proxy
  wallets; this is the correct observable identity.

## Endpoint audit checklist

| Endpoint | Purpose | Status |
|---|---|---|
| Data API `GET /trades` | Trade ingestion | ✅ Audited 2026-09-19 |
| Data API `GET /activity` | Redemptions/liquidity events for accounting | ⚠ PENDING-VPS |
| Data API `GET /positions` | Position reconciliation | ⚠ PENDING-VPS |
| Gamma API `GET /markets` | Market metadata + token mapping | ⚠ PENDING-VPS |
| CLOB API `GET /markets/{condition_id}` | Token mapping fallback | ⚠ PENDING-VPS |

## Fixed rules (not audit-dependent)

- Wallet addresses stored lowercase.
- Tradable identity is the CLOB token ID; outcome text is display data.
- `outcomeIndex` is never used as a key.
- Anything unverifiable from the sandbox is marked ⚠ PENDING-VPS and must be
  confirmed by `backend/scripts/probe_source_identity.py` before ingestion
  is considered live-ready.
