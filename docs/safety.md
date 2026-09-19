# Safety Model

Everything here is fail-closed: on error, ambiguity, or missing data, the
default is BLOCK. There is no "default allow" path.

## Layer 1 — Config (enforced at process startup, Chunk 1)

| Rule | Enforcement |
|---|---|
| Paper mode default | `POLYCOPY_PAPER_MODE=true` default |
| Kill switch default ON | `POLYCOPY_ORDER_KILL_SWITCH=true` default |
| No keys in paper mode | Private key + `ALLOW_LIVE_TRADING=false` → startup error |
| No ambiguous modes | `ALLOW_LIVE_TRADING=true` + `PAPER_MODE=true` → startup error |
| No live + kill switch | `ALLOW_LIVE_TRADING=true` + kill switch ON → startup error |

## Layer 2 — Code structure

There is **no live execution code in the repository** in Chunks 1–2. The bot
entrypoint exists but contains no trading logic. Live trading in Chunk 3
requires a new `ExecutionBroker` implementation behind
`POLYCOPY_ALLOW_LIVE_TRADING=true`, in its own reviewed PR.

## Layer 3 — Risk gates (Chunk 2, enforced on every order)

1. Kill switch (global)
2. Paper mode
3. Exposure limits: per-order, per-market, global (`POLYCOPY_MAX_*`)
4. Review delay before any fill (`POLYCOPY_REVIEW_DELAY_SECONDS`)

## Layer 4 — Data integrity

- Idempotency keys on orders (unique constraint).
- Unique external trade IDs (no double-ingestion).
- One settlement per market.
- Decision log: every action recorded with actor and context.

## Layer 5 — Visibility

- Dashboard banner always shows PAPER/LIVE mode and kill switch state,
  read from the API — never from static config.
- `/health/deps` reports Postgres and Redis connectivity.
- Structured JSON logs on every service.
