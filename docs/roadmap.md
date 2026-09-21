# Build Roadmap

## Chunk 1 — Scaffolding + corrections

- [x] Repo layout, Docker Compose stack, nginx
- [x] PostgreSQL schema (explicit frozen Alembic migration)
- [x] FastAPI: health, status, config, empty read endpoints
- [x] Bot skeleton (starts, logs, idles — no trading code)
- [x] React dashboard shell with live safety banner
- [x] CI: backend tests, frontend build, compose validation (`.github/workflows/ci.yml`)
- [x] Corrections: independent kill-switch gate, single wallet approval state,
      first-class CLOB token identity, capacity claims corrected
- [ ] **Nothing ingests. Nothing scores. Nothing trades.**

## Chunk 2 — Complete paper system

1. [x] **Source-identity audit (FIRST TASK)** — bounded audit of Polymarket
   Data/CLOB/Gamma APIs; fills in `docs/source-identity-contract.md`. No
   ingestion code before this contract is written down.
2. [x] Trade ingestion service (bounded batches, rate-limited, deduplicated)
3. [x] Settlement feed (realized outcomes backfill → real P&L)
4. [x] Wallet accounting (canonical trades → positions → settlements → validated
   wallet P&L) — validated BEFORE any scoring is trusted
5. [x] Wallet scoring V1 per `docs/wallet-intelligence.md` (five interpretable
   components, no Kelly/Sharpe in V1)
6. [x] Approval queue workflow (`discovered → pending_review → approved`)
7. [x] Tailing daemon (REST polling at `ingestion_poll_interval_seconds`;
   WebSocket upgrade deferred — evidence first)
8. [x] Realistic paper execution per `docs/paper-execution-model.md`
   (detection-lag → order book → slippage → partial/no-fill) — PR-E,
   incl. the PR #7 final hardening pass (review delay, asset_id token
   identity, defer-not-consume kill switch, execution-time approval
   recheck, fee-consistent accounting, malformed-book rejection,
   per-signal failure isolation, bounded cycles, paper settlement at
   resolution, populated-schema migration safety)
9. [ ] Dashboard wired to real data — PR-F (next)

## Chunk 3 — Live trading (separate, gated PR)

- `LiveExecutionBroker` behind `POLYCOPY_ALLOW_LIVE_TRADING=true`
- Boot posture: live-capable with kill switch ON, verified end-to-end
- Production secrets gate: generated Postgres password, restricted env file
  permissions, private-key storage, backup encryption, no secrets in images
  or git, credential rotation
- Ops gate: DB growth/disk monitoring, WAL management, log rotation,
