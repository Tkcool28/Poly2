# Safety Model

Everything here is fail-closed: on error, ambiguity, or missing data, the
default is BLOCK. There is no "default allow" path.

## Layer 1 — Config (enforced at process startup)

| Rule | Enforcement |
|---|---|
| Paper mode default | `POLYCOPY_PAPER_MODE=true` default |
| Kill switch default ON | `POLYCOPY_ORDER_KILL_SWITCH=true` default |
| No keys in paper mode | Private key + `ALLOW_LIVE_TRADING=false` → startup error |
| No ambiguous modes | `ALLOW_LIVE_TRADING=true` + `PAPER_MODE=true` → startup error |

## Kill switch semantics

`POLYCOPY_ORDER_KILL_SWITCH` is an **independent global execution gate**.
When ON it blocks ALL order creation — paper and live alike.

In the paper bot fresh blocked signals stay `pending`: no executable paper
fill and no CLOB book request. The bot counts kill-switch deferrals. Once the
source trade exceeds the configured five-minute execution age, the signal is
recorded as a **missed** paper opportunity (`stale_signal`), with no book
request or fill. Clearing the switch cannot execute an old backlog.
Before a controlled paper trial, inspect and record pending signals and
allow stale signals to be marked missed while the switch is still on.

It is intentionally legal to boot a live-capable system with the kill switch
ON:

```
POLYCOPY_ALLOW_LIVE_TRADING=true
POLYCOPY_PAPER_MODE=false
POLYCOPY_ORDER_KILL_SWITCH=true
```

That is the required staging posture: connect to the exchange, load
credentials, run reconciliation — execute nothing. Clearing the kill switch
is a separate, explicit operator action, never a side effect of config.

## Layer 2 — Code structure

There is **no live execution code in the repository** in Chunks 1–2. The bot
entrypoint exists but contains no trading logic. Live trading in Chunk 3
requires a new `LiveExecutionBroker` implementation behind
`POLYCOPY_ALLOW_LIVE_TRADING=true`, in its own reviewed PR.

## Layer 3 — Risk gates (Chunk 2, enforced on every order)

1. Kill switch (global execution gate)
2. Paper mode
3. Exposure limits: per-order, per-market, global (`POLYCOPY_MAX_*`)
4. Review delay before any fill (`POLYCOPY_REVIEW_DELAY_SECONDS`)

## Layer 4 — Data integrity

- Idempotency keys on orders (unique constraint).
- Unique canonical trade IDs (no double-ingestion; identity contract in
  `docs/source-identity-contract.md`).
- One settlement per market.
- Decision log: every action recorded with actor and context.

## Layer 5 — Visibility

- Dashboard banner always shows PAPER/LIVE mode and kill switch state,
  read from the API — never from static config.
- `/health/deps` reports Postgres and Redis connectivity.
- Structured JSON logs on every service.
