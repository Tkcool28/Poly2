# Architecture

```
Polymarket API ──> Trade Ingestion ──> PostgreSQL ──> Scoring Engine ──> Approval Queue
                        │                                        │
                        └──> Redis (signal buffer/dedup)         └──> Watchlist
                                                                        │
Dashboard (React) <── nginx <── FastAPI <── Tailing Bot <── approved wallets only
```

## Chunk 1 (current)

All services run and health-check; data stores are migrated but empty.
The bot process starts, logs its safe state, and idles — by design.

## Services

| Service | Role | Chunk 1 behavior |
|---|---|---|
| `postgres` | System of record | Schema applied via Alembic `migrate` job |
| `redis` | Signal buffer / dedup (Chunk 2) | Running, health-checked |
| `backend` | FastAPI | `/health`, `/health/deps`, `/system/status`, `/config`, empty `/wallets`, `/signals` |
| `bot` | Tailing daemon | Skeleton: starts, logs, idles. No ingestion, no execution |
| `frontend` | React dashboard | Shell pages + live safety banner from `/system/status` |
| `nginx` | Reverse proxy | `/api/*` → backend, everything else → frontend |
| `migrate` | One-shot Alembic job | Runs `alembic upgrade head` before backend starts |

## Data model

Tables: `wallets`, `markets`, `trades`, `settlements`, `wallet_scores`,
`signals`, `approval_queue`, `paper_orders`, `positions`, `decision_log`,
`service_heartbeats`.

Key constraints baked into the schema:

- `trades.polymarket_trade_id` UNIQUE — idempotent ingestion, duplicates
  can never double-count.
- `paper_orders.idempotency_key` UNIQUE — duplicate submissions replay, never
  double-execute.
- `settlements.market_id` UNIQUE — a market settles exactly once.
- `wallets.address` UNIQUE — canonical (lowercase) address normalization.

## Why PostgreSQL (not SQLite)

The v1 incident: SQLite under concurrent ingestion + API load exhausted VPS
disk and memory. Postgres gives real concurrency (MVCC), connection pooling,
and partial indexes. Combined with the ingestion bounds
(`POLYCOPY_INGESTION_BATCH_SIZE`, `POLYCOPY_INGESTION_MAX_CONCURRENT_REQUESTS`),
memory footprint stays bounded regardless of data volume.
