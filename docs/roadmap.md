# Poly2 roadmap and deployment state

**Deployed baseline (2026-09-24):** production main
`a3dd0469b9e3c1be2185f05f50a96b0ba21ecaac` after PR #15. The dashboard,
ingestion, scoring, approval, and paper execution components exist. Draft PRs
#16–#19 propose the next integration milestone; their behavior is not production
behavior until independently reviewed, merged, and deployed.

## Chunk 1 — Scaffolding + corrections

- [x] Repo layout, Docker Compose stack, nginx
- [x] PostgreSQL schema (explicit frozen Alembic migration)
- [x] FastAPI: health, status, config, empty read endpoints
- [x] Bot skeleton (starts, logs, idles — no trading code)
- [x] React dashboard shell with live safety banner
- [x] CI: backend tests, frontend build, compose validation (`.github/workflows/ci.yml`)
- [x] Corrections: independent kill-switch gate, single wallet approval state,
      first-class CLOB token identity, capacity claims corrected
- [x] Historical Chunk 1 checkpoint: no ingestion, scoring, or trading was
      enabled at that stage. Chunk 2 implemented these features later.

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
9. [x] Dashboard wired to PostgreSQL-backed wallet, approval, signal, position,
   and health APIs; deployed before this milestone.

## Autonomous paper readiness (draft PR stack; not yet deployed)

1. [ ] PR #16 — bounded approved-wallet continuity recovery and deeper,
   goal-aware candidate history bootstrap.
2. [ ] PR #17 — persistent settlement scheduling, Gamma cooldown, hourly
   automatic candidate scoring. Migration `0008`.
3. [ ] PR #18 — source-to-paper maximum age, stale misses, durable deferrals.
   Migration `0009`.
4. [ ] PR #19 — evidence API/dashboard, independent watchdog, paper run ledger,
   end-to-end harness. Migration `0010`.
5. [ ] Independent reviews and merges in order; deploy with paper mode ON,
   live trading OFF, kill switch ON, and no private key.
6. [ ] Re-run full suite on **combined merged main**, migrate, verify startup,
   heartbeats, watchdog, and backlog; record deployed SHA and evidence log.
7. [ ] After separate operator authorization, perform a controlled real-source
   paper test. If no suitable new source trade occurs, record that limit and
   rely on the deterministic harness. Real settlement/P&L may take longer than
   the test window; the milestone remains unaccepted until actually verified.

## Chunk 3 — Live trading (separate, gated PR)

- `LiveExecutionBroker` behind `POLYCOPY_ALLOW_LIVE_TRADING=true`
- Boot posture: live-capable with kill switch ON, verified end-to-end
- Production secrets gate: generated Postgres password, restricted env file
  permissions, private-key storage, backup encryption, no secrets in images
  or git, credential rotation
- Ops gate: DB growth/disk monitoring, WAL management, log rotation,
  backup/restore drills, retention policy
- Order cancellation, execution reconciliation, recovery behavior, alerts,
  canary deployment, live-readiness review
- Only after sustained profitable paper results justify it
