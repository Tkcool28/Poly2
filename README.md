# Polycopy (rebuild)

Smart-wallet discovery, scoring, and copy-trading for Polymarket.
**Paper-first, fail-closed.** This rebuild replaces the original SQLite-based
Polycopy with a PostgreSQL + Redis + event-driven architecture.

> **Chunk 1 status:** scaffolding only. The full stack runs (Postgres, Redis,
> FastAPI, React dashboard, nginx, bot skeleton) but nothing ingests data,
> scores wallets, or trades — that lands in Chunk 2. There is **no live
> trading code anywhere in this repo.**

## Quick start

```bash
cp .env.example .env
docker compose up --build
# Dashboard: http://localhost/
# API:       http://localhost/api/health
```

## Stack

| Piece | Tech |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic |
| Database | PostgreSQL 15 |
| Cache/queue | Redis 7 |
| Bot | asyncio daemon (skeleton in Chunk 1) |
| Frontend | React 18, TypeScript, Tailwind CSS, Vite |
| Infra | Docker Compose, nginx, systemd unit |

## Safety model (non-negotiable)

- Paper mode is the default; the kill switch defaults **ON**.
- Config **refuses to start** if a private key is present while
  `POLYCOPY_ALLOW_LIVE_TRADING=false`.
- Live trading does not exist until Chunk 3, gated behind
  `POLYCOPY_ALLOW_LIVE_TRADING=true` and a working `ExecutionBroker`.
- Ingestion (Chunk 2) has hard batch-size and concurrency caps so the
  database can never be hammered into OOM.

See `docs/safety.md` and `docs/architecture.md`.

## Development

```bash
# Backend tests
cd backend && pip install -e ".[dev]" && pytest

# Frontend dev server (proxies /api to localhost:8000)
cd frontend && npm install && npm run dev
```

## Roadmap

1. **Chunk 1 (this):** scaffolding — stack wired, schema migrated, CI green. Nothing live.
2. **Chunk 2:** ingestion + settlement feed + scoring v2 + paper copy bot.
3. **Chunk 3:** live trading behind explicit gates, only after paper results justify it.

## License

MIT
