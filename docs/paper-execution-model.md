# Paper Execution Model

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

- `t₀`, `t₁`, `t₂` timestamps (source trade, detection, decision)
- source wallet's price vs our simulated fill price (slippage vs source)
- book depth consumed; whether fill was full, partial, or missed
- the signal's age at execution

These fields feed the evidence loop in `docs/wallet-intelligence.md` §6 —
they are how we learn whether the strategy survives contact with reality.

## Fees

Paper fills apply the configured fee rate. If Polymarket's fee model changes,
this model changes with it — fees are config, not constants.

## What this model deliberately does NOT assume

- That our order would have zero market impact (at small size this is nearly
  true, but the book-walk still prices it).
- That detection is instant. Detection lag is measured and reported, not
  assumed away.
- That every signal fills. Missed fills are data.
