# Wallet Intelligence — What a "Smart Wallet" Is and How We Find One

This document is the permanent reference for wallet discovery, scoring, and
copyability. It exists so that any component in the system can be traced back
to a reason. If a metric or filter can't explain which failure mode it kills,
it doesn't belong here.

## 1. Definition

> **A "smart wallet" is one with verified, settlement-based profit across
> enough independent markets that luck is an unlikely explanation, trading in
> a style we can actually copy.**

Three required parts: real profit, statistical believability, copyability.

## 2. Discovery Sources

| Source | How | Role |
|---|---|---|
| Polymarket leaderboard | Public API, P&L-ranked, day/week/month windows | Candidate generation |
| Whale trade feed | Large trades on active markets | Early candidate detection |
| Manual watchlist | Operator-provided addresses | Never auto-scored away |
| Related-wallet heuristic | Shared funding sources, timing clusters | Flag only, never auto-approve |

Every candidate enters `approval_queue` as `discovered`. Nothing trades off a
discovered wallet.

## 3. Eligibility Gates (run before scoring — cheap yes/no checks)

- **≥ 15 settled markets** (unsettled P&L is a guess)
- **≥ 30 total trades**
- **Account age ≥ 30 days**
- **Active within the last 14 days**
- **Positive realized P&L** after fees

**Scoring input caveat:** gates and scores use locally ingested trades only.
Recurring ingestion requests the most recent `POLYCOPY_INGESTION_BATCH_SIZE`
(500) trades per wallet per cycle. Approved wallets also persist a continuity
anchor: if the latest slice no longer contains the previously observed trade,
at most `POLYCOPY_CATCH_UP_PAGES_PER_CYCLE` (3) older pages are processed per
cycle until that canonical trade reappears. The offset, timestamp window, and
incomplete state survive restarts in the decision log. A lost upstream window
remains visibly incomplete; a full newest page never proves continuity.

The bot owns candidate rescoring on an independent 3,600-second cadence; the
operator scoring endpoint remains available. Neither path can approve a wallet.
Before a scoring pass, discovered candidates use a separate historical
bootstrap (100 trades per page; 25 pages per run; cumulative caps of 200 pages,
20,000 fetched rows, 240 logical requests, and 20 Gamma market checks). It
pages newest-first using Data API `offset`, `start=1`, and a fixed `end` timestamp.
At the API's 10,000-offset ceiling it rolls to an older inclusive timestamp
window and resets the offset; canonical trade identity deduplicates boundary
overlap. If all rows at the boundary share one second, the scan fails closed
when an older window cannot be established. Settlement reconciliation uses
existing closed Gamma market validation and stops requesting settlements once
the 15-market maturity gate is met. It stops when the 30
trade, 15 settled-market, and 30-day age gates can be evaluated, the upstream
history ends, or a cumulative bound is reached. A per-run page cap records
`in_progress` and delays scoring until a subsequent pass finishes. The
`/wallets/{id}/bootstrap` endpoint exposes termination and evidence. This
bounded pass does not claim complete wallet history.

Unresolved markets have persisted next-check times (one-minute initial retry,
exponential backoff capped at 12 hours). The bot checks at most ten due markets
per cycle; resolved markets are excluded permanently. Gamma 429 responses honor
bounded `Retry-After` cooldowns, persisted across bot and API workers for the
Gamma settlement family. A cooldown never counts as settlement evidence.

Gate failures split into two kinds (review correction, 2026-09-20):

- **Maturity gates** (settled markets, trade count, account age): failing
  means "not enough evidence yet" → verdict `insufficient_history`. The
  wallet STAYS `discovered` and is automatically rescored on future
  cycles. Lack of evidence is never a terminal judgment.
- **Evidence gates** (dormant >14d, non-positive realized P&L *after*
  adequate history): failing → machine verdict `score_rejected`.

Machine verdicts are recorded in the WalletScore breakdown and decision
log only. The scorer NEVER writes `rejected` to `wallets.approval_state` —
that state is reserved for human decisions made via the API.

## 4. V1 Score — Five Interpretable Components (0–100)

| Component | Weight | Failure mode it kills |
|---|---|---|
| Sample strength (settled-market count, linear to 50) | 25% | Tiny-sample "geniuses" |
| Concentration (max % of P&L from one market; hard reject if >50%) | 20% | One-lucky-bet / one-insider-trade wallets |
| Profit factor (gross wins ÷ gross losses, settled only) | 20% | Wins often but blows up bigger |
| Consistency (% of profitable decision-weeks while active) | 20% | One good month, then nothing |
| Recency (% of lifetime P&L from decisions in last 30d) | 15% | Wallets whose edge died months ago |

**Absolute dollar P&L is deliberately NOT a component** (review
correction, 2026-09-20): it conflates skill with bankroll size. A wallet
up $300 on a $500 bankroll may be smarter than one up $10k on $100k.
Positive realized P&L stays as an eligibility gate; the 25% component
rewards evidence depth instead. Naive ROI was considered and rejected —
capital flows, overlapping positions, and partial exits make it
unreliable; a bad ROI metric is worse than none.

**Recency and consistency use DECISION time, not settlement time:**
a market's P&L is attributed to the wallet's first trade in it.
Resolution determines when a result becomes knowable; trade timing
determines when the decision was made. A 6-month-old decision that
settled yesterday is NOT recent performance.

**Consistency limitation (documented, accepted for V1):** it measures
consistency *while active*, not continuous weekly persistence — three
profitable weeks six weeks apart reads as 3/3. Dormancy is caught by the
14-day inactivity gate instead.

Verdicts: ≥70 → `pending_review` (human approves) · 50–69 → stays
`discovered` · <50 → `score_rejected` (machine; wallet remains in the
rescan pool). Thresholds are provisional and untuned — tuning comes from
prospective paper evidence (§6), never retrospective optimization.

**Stale queue entries are withdrawn automatically** (review correction,
2026-09-20): if a wallet in `pending_review` is rescored and the new
verdict is `discovered`, `score_rejected`, or `insufficient_history`,
its approval state returns to `discovered` and the open queue entry is
closed as `withdrawn`. A human must never approve off an outdated score.

No Kelly criterion, no Sharpe ratio, no behavioral clustering in V1. Those are
add-later candidates, added only when paper evidence shows a specific weakness.

## 5. Copyability — a Separate Score from Smartness

A wallet can be genuinely smart and uncopyable. Evaluated before any paper
order:

- **Entry timing** — trades hours/days before resolution are copyable; final
  minutes means the price already moved.
- **Liquidity** — book depth at our order size when the signal fires.
- **Price zone** — entries at 0.90+ leave ~10% max upside; usually not worth
  fees + slippage.
- **Detection lag** — we timestamp source-trade time vs detection time. Paper
  fills are simulated at the *detection-time* order book, never the source
  wallet's price.

## 6. Evidence Loop (post-Chunk-2 iteration)

1. V1 filter → approve a handful of wallets → paper-copy for 1–2 weeks.
2. Track per wallet: copied-trade hit rate, realized slippage vs source price,
   copied P&L vs source P&L on the same trades, detection-latency distribution.
3. Diagnose the dominant loss source (slippage? bad category? timing?).
4. Add exactly one feature targeting it. Re-measure. Repeat.

## Known Traps (bake these into everything)

- Leaderboard rank rewards size and luck as much as skill — it's a candidate
  generator, not a score.
- Specialization beats overall rank — niche dominance (weather, geopolitics)
  usually indicates real edge; overall rank mixes it with noise.
- Market makers look great on volume and win rate but are terrible copy
  targets (tiny margins, both sides).
- One large winning trade (insider or luck) dominates naive P&L — the
  concentration filter exists for exactly this.
- Polymarket accounting has quirks: negative-risk markets, merges/splits,
  hedged YES+NO positions, redemptions. Wallet P&L reconstruction must be
  validated before any score is trusted.
