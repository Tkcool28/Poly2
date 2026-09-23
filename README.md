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
# Set POLYCOPY_POSTGRES_USER, POLYCOPY_POSTGRES_PASSWORD, and
# POLYCOPY_DATABASE_URL in .env (using the same database credentials).
docker compose up --build
# Dashboard: http://127.0.0.1:8790/
# API:       http://127.0.0.1:8790/api/health
```

## Production ingress

Compose publishes nginx only on `${POLYCOPY_BIND_ADDRESS:-127.0.0.1}:${POLYCOPY_HTTP_PORT:-8790}:80`.
On the VPS, host Caddy owns ports 80/443, terminates HTTPS and authentication,
and proxies to `http://127.0.0.1:8790` by default. Keep the bind address on
loopback; set `POLYCOPY_HTTP_PORT` in `.env` if that port is occupied. Caddy
must forward the original `/api/` path unchanged. Existing nginx routing strips
the `/api/` prefix and forwards to the backend's bare FastAPI routes.

Set `POLYCOPY_POSTGRES_USER` and `POLYCOPY_POSTGRES_PASSWORD` in `.env`; the
database name is `POLYCOPY_POSTGRES_DB` (default `polycopy`). Set
`POLYCOPY_DATABASE_URL` to
`postgresql+asyncpg://<URL-encoded user>:<URL-encoded password>@postgres:5432/<database>`
with the same values. URL-encode special characters in the username/password.
The example intentionally contains no working database credentials. For an
already initialized Postgres volume, changing these initialization variables
does not change existing database roles or passwords; provision matching
credentials before switching the application URL.

After `infra/deploy.sh` reports success, validate through the authenticated
host Caddy endpoint (with the appropriate credentials):

```text
/api/health
/api/health/deps
/api/system/status
/api/wallets
/api/signals
/api/positions
/api/approval-queue
```

Production reboot, update, backup, restore-drill, logging, and systemd
procedures are in [production operations](docs/production-operations.md).

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
