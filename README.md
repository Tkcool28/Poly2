# Polycopy (rebuild)

Smart-wallet discovery, scoring, and paper copy-trading for Polymarket.
**Paper-first, fail-closed.** PostgreSQL owns trades, scores, signals,
positions, and deduplication; Redis remains future infrastructure.

> **Production baseline (2026-09-24):** main
> `a3dd0469b9e3c1be2185f05f50a96b0ba21ecaac` has candidate bootstrap,
> scoring, human approval, bounded bot ingestion, paper execution, dashboard,
> and a kill switch that remains ON. Draft PRs #16–#19 propose the additional
> paper readiness integration; they are not merged or deployed. There is no
> live trading code in this repository.

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
/api/paper/evidence                 # after proposed PR #19 is deployed
/api/paper/backlog                  # after proposed PR #19 is deployed
```

Production reboot, update, backup, restore-drill, logging, and systemd
procedures are in [production operations](docs/production-operations.md).

## Stack

| Piece | Tech |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic |
| Database | PostgreSQL 15 |
| Future cache/queue | Redis 7 (no signal ownership today) |
| Bot | Bounded asyncio ingestion, settlement, scoring, paper execution |
| Frontend | React 18, TypeScript, Tailwind CSS, Vite |
| Infra | Docker Compose, nginx, systemd unit |

## Safety model (non-negotiable)

- Paper mode is the default; the kill switch defaults **ON**.
- Config **refuses to start** if a private key is present while
  `POLYCOPY_ALLOW_LIVE_TRADING=false`.
- Live trading does not exist until Chunk 3, gated behind
  `POLYCOPY_ALLOW_LIVE_TRADING=true` and a working `ExecutionBroker`.
- Ingestion has per-cycle batch/concurrency limits; the proposed bounded
  recovery and candidate-bootstrap paths also have separate hard caps.

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
