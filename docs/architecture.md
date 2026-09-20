# Architecture

```
Polymarket API ──> Trade Ingestion ──> PostgreSQL ──> Scoring Engine ──> Approval Queue
                        │                                        │
                        └──> Redis (signal buffer/dedup)         └──> Watchlist
                                                                        │
Dashboard (React) <── nginx <── FastAPI <── Tailing Bot <── approved wallets only
```

## Chunk 2 (current)

Ingestion, accounting, scoring, and approval are live in paper mode. The
bot daemon runs a bounded cycle every `ingestion_poll_interval_seconds`:
ingest → detect signals from approved wallets → simulate fills against the
detection-time order book → heartbeat. No live trading paths exist.

## Services

| Service | Role | Chunk 2 behavior |
|---|---|---|
| `postgres` | System of record | Schema applied via Alembic `migrate` job |
| `redis` | Signal buffer / dedup (future) | Running, health-checked |
| `backend` | FastAPI | `/health`, `/system/status`, `/config`, `/wallets`, `/signals`, `/approval-queue`, score + approve/reject/disable endpoints |
| `bot` | Tailing daemon | Poll → ingest → signal → paper fill loop; paper only, kill-switch gated |
| `frontend` | React dashboard | Shell pages + live safety banner from `/system/status` |
| `nginx` | Reverse proxy | `/api/*` → backend, everything else → frontend |
| `migrate` | One-shot Alembic job | Runs `alembic upgrade head` before backend starts |

## Data model

Tables: `wallets`, `markets`, `trades`, `settlements`, `wallet_scores`,
`signals`, `approval_queue`, `paper_orders`, `positions`, `decision_log`,
`service_heartbeats`.

Key constraints baked into the schema:

- `trades.polymarket_trade_id` UNIQUE — idempotent ingestion, duplicates
  can never double-count. What goes into this column is governed by
  `docs/source-identity-contract.md` (written before ingestion ships).
- `paper_orders.idempotency_key` UNIQUE — duplicate submissions replay, never
  double-execute.
- `signals.source_trade_id` UNIQUE — a source trade produces at most one
  signal, ever (PR-E).
- `settlements.market_id` UNIQUE — a market settles exactly once.
- `wallets.address` UNIQUE — canonical (lowercase) address normalization.
- `markets.clob_token_ids` / `trades.asset_id` — the tradable identity on
  Polymarket is the CLOB token, not human-readable outcome text.
- Wallet approval has a single source of truth: `approval_state`.

## Why PostgreSQL (not SQLite)

The v1 incident: SQLite under concurrent ingestion + API load exhausted VPS
disk and memory. Postgres gives real concurrency (MVCC), connection pooling,
and the indexing we need.

**What Postgres does NOT do:** bound resource usage by itself. Connection
pool limits (`POLYCOPY_DB_POOL_SIZE`, `POLYCOPY_DB_MAX_OVERFLOW`) and the
Chunk 2 ingestion bounds (`POLYCOPY_INGESTION_BATCH_SIZE`,
`POLYCOPY_INGESTION_MAX_CONCURRENT_REQUESTS`) cap *pressure per moment*, not
total growth. Before Chunk 3 we still need: query timeouts, database growth
and disk-space monitoring, WAL management, log rotation, backup/restore
procedures, and a retention policy. Tracked in `docs/roadmap.md`.
