# Paper Execution Model

> **Implemented in PR-E** (`backend/src/polycopy/execution/`): `bookwalk.py`
> holds the pure fill math; `service.py` does signal detection and order
> simulation. Every paper order carries the full detection-time book
> snapshot so sizing/gate changes can be evaluated offline later.

## Principle

> Paper trading answers: **"What could Poly2 realistically have executed at
> the moment Poly2 learned about the trade?"** — never "what did the source
> wallet get?"

A paper system that copies the source wallet's fill price produces
optimistic fiction, and any Chunk 3 go/no-go decision built on it would be
worthless.

## The realistic fill pipeline

```
source trade observed on-chain/API
    │  (t₀ = traded_at from source)
    ▼
detection (t₁ = when our ingestion saw it; detection lag = t₁ − t₀)
    ▼
decision (t₂ = risk gates + copyability check pass)
    ▼
market snapshot at t₂: current order book for the CLOB token
    ▼
simulated fill:
    • walk the book from the top — our size fills level by level
    • slippage = volume-weighted fill price vs top-of-book
    • partial fill if book depth < our size
    • NO fill if spread/depth fails minimums (a real outcome, recorded as such)
    ▼
paper order (status: filled / partial / missed) → paper position
```

## Recording requirements

Every paper order records, for later analysis:

- `t₀`, `t₁`, `t₂` timestamps (source trade, detection, decision) — `t₁`
  is the trade's `ingested_at` (honest detection time), never the time a
  later detection query happened to run
- source wallet's price vs our simulated fill price (slippage vs source)
- depth available on our side of the book (`depth_available` — evidence
  for future sizing; what we consumed is `filled_size`)
- whether fill was full, partial, or missed
- the signal's age at execution

These fields feed the evidence loop in `docs/wallet-intelligence.md` §6 —
they are how we learn whether the strategy survives contact with reality.

## Fees

Paper fills apply the configured fee rate (`POLYCOPY_PAPER_FEE_RATE`,
default 0). If Polymarket's fee model changes, this model changes with
it — fees are config, not constants.

Fee accounting is internally consistent (PR #7 hardening): a BUY fee goes
INTO cost basis (`avg_price`), a SELL fee comes OUT of realized P&L, and
settlement realizes against that same fee-inclusive basis — `PaperOrder.fee`
and portfolio P&L can never disagree. A nonzero-fee BUY→SELL round-trip
regression pins the exact arithmetic.

## Hardening rules (PR #7 final pass)

- **Review delay** — a signal is not eligible until
  `t₁ + POLYCOPY_REVIEW_DELAY_SECONDS`; before that it stays pending
  (no order, no book request).
- **Token identity** — the book request uses the source trade's
  `asset_id` (carried onto the signal). Gamma outcome→token metadata is
  fallback evidence only; it may be missing or stale.
- **Kill switch** — defers, never consumes: signal stays pending, no
  PaperOrder, no CLOB request; it becomes eligible when the switch
  clears (see `docs/safety.md`).
- **Approval recheck** — wallet approval is re-validated at execution
  time; a wallet disabled during the review delay → missed
  (`wallet_not_approved`), no book request.
- **Paper-only invariant** — the execution module fails closed
  (`RuntimeError`) in any non-paper runtime. No live path exists.
- **Malformed books** — levels with price ∉ (0, 1], size ≤ 0, or
  non-finite values are rejected before the walk (parse layer AND inside
  `walk_book`). Bad upstream data can never crash a cycle or produce an
  invalid VWAP.
- **Failure isolation** — one failing signal rolls back its own
  transaction, is logged, and the cycle continues; idempotency keys
  prevent duplicates on retry.
- **Cycle bounds** — detection ≤ `POLYCOPY_SIGNAL_DETECTION_BATCH_SIZE`
  (200), executions ≤ `POLYCOPY_EXECUTION_BATCH_SIZE` (50) book requests
  per cycle, oldest first. A backlogged DB can never become an execution
  storm.

## Paper settlement at resolution

Paper P&L cannot depend on the source wallet sending a SELL. When a
market resolves (a `Settlement` row exists), any still-open paper
position realizes exactly once: winning shares at $1, losing shares at
$0, against the fee-inclusive cost basis. `positions.settled_at` is the
idempotency marker — repeated settlement cycles are no-ops. This runs
inside `run_execution_cycle`, right after settlement refresh.

## Sizing (V1)

Fixed `POLYCOPY_MAX_ORDER_SIZE_USD` (default **$10**) per signal, capped
by per-market and global exposure limits (TK decision, 2026-09-20). We
deliberately do NOT size off the source wallet's bankroll: the paper
phase exists to measure slippage, hit rate, and detection lag — the
recorded book snapshots are the evidence base for evaluating larger
sizing later.

## What this model deliberately does NOT assume

- That our order would have zero market impact (at small size this is nearly
  true, but the book-walk still prices it).
- That detection is instant. Detection lag is measured and reported, not
  assumed away.
- That every signal fills. Missed fills are data.
