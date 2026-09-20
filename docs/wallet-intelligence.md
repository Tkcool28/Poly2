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
