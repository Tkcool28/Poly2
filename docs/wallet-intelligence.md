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

Fail any gate → `rejected`, with the reason recorded in the decision log.

## 4. V1 Score — Five Interpretable Components (0–100)

| Component | Weight | Failure mode it kills |
|---|---|---|
| Realized P&L quality (settlement-based, fee-adjusted) | 25% | Fake profit from price-direction heuristics |
| Concentration (max % of P&L from one market; hard reject if >50%) | 20% | One-lucky-bet / one-insider-trade wallets |
| Profit factor (gross wins ÷ gross losses, settled only) | 20% | Wins often but blows up bigger |
| Consistency (% of profitable weeks while active) | 20% | One good month, then nothing |
| Recency (time-decayed recent vs lifetime performance) | 15% | Wallets whose edge died months ago |

Verdicts: ≥70 → `pending_review` (human approves) · 50–69 → stays
`discovered` · <50 → `rejected`.

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
