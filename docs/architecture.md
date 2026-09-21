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
ingest → refresh settlements → settle paper positions → detect signals
from approved wallets → execute eligible signals against the
detection-time order book → heartbeat. No live trading paths exist.

Runtime ownership (PR #7 hardening — the concrete autonomous path):

| Concern | Runtime owner |
|---|---|
| Ingestion (`run_ingestion_cycle`) | **bot daemon**, every cycle |
| Settlement refresh (`refresh_settlements`) | **bot daemon**, every cycle |
| Paper settlement (`settle_paper_positions`) | **bot daemon**, inside `run_execution_cycle` |
| Signal detection + paper execution | **bot daemon**, inside `run_execution_cycle` |
| Wallet scoring (`score_all_wallets`) | **API**: `POST /scoring/run` (operator or cron) |
| Human approval | **API**: `POST /wallets/{id}/{action}` |

Candidate-wallet DISCOVERY is intentionally still manual in Chunk 2:
wallets are added explicitly, then scored, reviewed, and approved by a
human. The full loop `ingest → settle → score → pending review → human
approval → tail → paper execute → paper settle` has a named owner at
every step.

## Services

| Service | Role | Chunk 2 behavior |
|---|---|---|
| `postgres` | System of record | Schema applied via Alembic `migrate` job |
| `redis` | Signal buffer / dedup (future) | Running, health-checked |
| `backend` | FastAPI | `/health`, `/system/status`, `/config`, `/wallets`, `/signals`, `/approval-queue`, score + approve/reject/disable endpoints |
| `bot` | Tailing daemon | Poll → ingest → settle → signal → paper fill loop; paper only, kill-switch gated |
| `frontend` | React dashboard | Shell pages + live safety banner from `/system/status` |
| `nginx` | Reverse proxy | `/api/*` → backend, everything else → frontend |
| `migrate` | One-shot Alembic job | Runs `alembic upgrade head` before backend starts |

## Data model

Tables: `wallets`, `markets`, `trades`, `settlements`, `wallet_scores`,
