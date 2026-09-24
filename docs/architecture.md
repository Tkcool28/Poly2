# Architecture

Polymarket Data/Gamma/CLOB APIs feed the bot; PostgreSQL stores the canonical
trades, settlement evidence, scores, approval states, signals, paper orders,
positions, decisions, and heartbeat state. FastAPI serves read/approval APIs
through nginx to the React dashboard. Redis runs as future infrastructure;
it does not buffer signals or own deduplication in this milestone.

**Authority:** deployed production main remains
`a3dd0469b9e3c1be2185f05f50a96b0ba21ecaac` until the separately
reviewed PRs #16–#19 are merged and deployed. The behavior below describes
the proposed combined code, not the currently deployed VPS.

## Chunk 2 (current)

Ingestion, accounting, scoring, and approval are live in paper mode. The
bot daemon runs a bounded cycle every `ingestion_poll_interval_seconds`:
ingest → refresh settlements → settle paper positions → detect signals
from approved wallets → execute eligible signals against the
detection-time order book → heartbeat. No live trading paths exist.

Runtime ownership (PR #7 hardening — the concrete autonomous path):

| Concern | Runtime owner |
|---|---|
| Ingestion (`run_ingestion_cycle`) | **bot daemon**, every cycle |
| Due settlement refresh (`refresh_settlements`) | **bot daemon**, at most 10 markets per cycle, persisted backoff |
| Paper settlement (`settle_paper_positions`) | **bot daemon**, inside `run_execution_cycle` |
| Signal detection + paper execution | **bot daemon**, inside `run_execution_cycle` |
| Wallet scoring (`score_all_wallets`) | **bot daemon**, every 3600 seconds; API `POST /scoring/run` remains an optional operator override |
| Human approval | **API**: `POST /wallets/{id}/{action}` |
| Successful-cycle watchdog | **independent watchdog container**, reads Postgres and exposes Docker health failure |

Candidate-wallet DISCOVERY is intentionally still manual in Chunk 2:
wallets enter via `POST /wallets` (dashboard "Add wallet" form on the
Wallets tab), then are scored, reviewed, and approved by a human. New
wallets land in `discovered` — bounded history bootstrap provides scoring data, but
nothing becomes copyable before human approval. The full loop `ingest → settle → score → pending review → human
approval → tail → paper execute → paper settle` has a named owner at
every step.

## Services

| Service | Role | Chunk 2 behavior |
|---|---|---|
| `postgres` | System of record | Schema applied via Alembic `migrate` job |
| `redis` | Signal buffer / dedup (future) | Running, health-checked |
| `watchdog` | Independent bot-success check | Alerts and becomes unhealthy if successful cycles stop |
| `backend` | FastAPI | `/health`, `/system/status`, `/config`, `/wallets`, `/signals`, `/approval-queue`, score + approve/reject/disable endpoints |
| `bot` | Tailing daemon | Poll → ingest → settle → signal → paper fill loop; paper only, kill-switch gated |
| `frontend` | React dashboard | Shell pages + live safety banner from `/system/status` |
| `nginx` | Reverse proxy | `/api/*` → backend, everything else → frontend |
| `migrate` | One-shot Alembic job | Runs `alembic upgrade head` before backend starts |

## Data model

Tables: `wallets`, `markets`, `trades`, `settlements`, `api_throttles`, `wallet_scores`,
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

Approved-wallet continuity and candidate-bootstrap cursors are append-only
`decision_log` checkpoints. Canonical IDs and database uniqueness remain the
deduplication authority. Gamma settlement scheduling lives on `markets`, with
cross-process 429 cooldown in `api_throttles`. Source trades become signals
only after a human-approved `approved_at` boundary. Five-minute source-to-book
freshness and the 30-second review delay gate paper execution; the kill switch
remains ON in production. Paper evidence is queried from PostgreSQL and
summarized for humans in `reports/PAPER_TRADING_RUN_LOG.md`.

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
