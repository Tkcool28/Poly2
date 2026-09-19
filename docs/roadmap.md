# Build Roadmap

## Chunk 1 — Scaffolding (this branch)

- [x] Repo layout, Docker Compose stack, nginx
- [x] PostgreSQL schema (Alembic) for the full vision
- [x] FastAPI: health, status, config, empty read endpoints
- [x] Bot skeleton (starts, logs, idles — no trading code)
- [x] React dashboard shell with live safety banner
- [x] CI: backend tests, frontend build, compose validation
- [ ] **Nothing ingests. Nothing scores. Nothing trades.**

## Chunk 2 — Paper copy bot

- Trade ingestion service (bounded batches, rate-limited, deduplicated)
- Settlement feed (realized outcomes backfill → real P&L)
- Wallet scoring engine v2: Sharpe, max drawdown, profit factor, Kelly,
  time-decay weighting, behavioral clustering
- Approval queue workflow (wallet `discovered → pending_review → approved`)
- Event-driven tailing daemon (WebSocket + REST hybrid, sub-10s detection)
- Paper execution through risk gates; dashboard wired to real data

## Chunk 3 — Live trading (separate, gated PR)

- `ExecutionBroker` implementation behind `POLYCOPY_ALLOW_LIVE_TRADING=true`
- Only after sustained profitable paper results and a readiness review
