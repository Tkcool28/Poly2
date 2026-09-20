"""Pure scoring logic — no DB, no network. Every number explainable.

Implements docs/wallet-intelligence.md §3–4, revised after review:

* Maturity gate failures (settled-market count, trade count, account age)
  produce verdict ``insufficient_history`` — NOT a rejection. The wallet
  stays in the automatic rescan pool. Lack of evidence is not evidence
  of badness.
* Evidence failures (dormant wallet, non-positive P&L after adequate
  history, concentration hard reject, composite < 50) produce
  ``score_rejected`` — a MACHINE verdict recorded in the score row and
  decision log. The scorer never writes ``rejected`` to
  ``wallets.approval_state``; that state means "a human rejected this".
* Absolute dollar P&L is NOT a score component — it conflates skill with
  bankroll size. Replaced by ``sample_strength`` (evidence depth), which
  is size-neutral. Positive realized P&L remains an eligibility gate.
* Recency uses DECISION time (the wallet's first trade in a market), not
  settlement time: resolution determines when a result becomes knowable;
  trade timing determines when the decision was made.

The functions consume a plain ``WalletStats`` snapshot and produce a
``ScoreResult`` with a full component breakdown, so "why did the system
score this wallet 63?" is always answerable from the stored JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

# --- Eligibility gates (docs/wallet-intelligence.md §3) -------------------
MIN_SETTLED_MARKETS = 15
MIN_TRADES = 30
MIN_ACCOUNT_AGE_DAYS = 30
MAX_INACTIVE_DAYS = 14
CONCENTRATION_HARD_REJECT = Decimal("0.50")

# --- Verdict thresholds (§4) — provisional, tuned by paper evidence later --
VERDICT_REVIEW_THRESHOLD = 70.0
VERDICT_REJECT_THRESHOLD = 50.0

WEIGHTS = {
    "sample_strength": 0.25,
    "concentration": 0.20,
    "profit_factor": 0.20,
    "consistency": 0.20,
    "recency": 0.15,
}

# Sample strength scale: 15 settled markets (the gate minimum) ≈ 30,
# 50+ → 100. Linear, size-neutral; the curve is a V1 hypothesis.
_SAMPLE_STRENGTH_FULL = 50
# Profit factor cap: pf ≥ 5 → 100 (beyond that it's sample-size luck).
_PROFIT_FACTOR_CAP = 5.0

# Gate kinds: "maturity" = not enough evidence yet (rescan later);
# "evidence" = actual negative signal.
MATURITY_GATES = {"settled_markets", "trades", "account_age"}
EVIDENCE_GATES = {"inactive", "realized_pnl", "no_history"}


@dataclass
class WalletStats:
    """Everything V1 scoring needs, pre-assembled. Decimals, not floats."""

    realized_pnl: Decimal
    settled_market_count: int
    trade_count: int
    gross_profit: Decimal
    gross_loss: Decimal
    per_market_pnl: list[Decimal]  # realized P&L per settled market
    weekly_pnl: list[Decimal]  # realized P&L bucketed by DECISION week
    recent_30d_pnl: Decimal  # realized P&L decided in the last 30 days
    first_trade_at: datetime | None
    last_trade_at: datetime | None


@dataclass
class ScoreResult:
    eligible: bool
    gate_failures: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    composite: float | None = None  # 0–100, None if not scored
    # pending_review | discovered | score_rejected | insufficient_history
    verdict: str = "insufficient_history"
    hard_reject_reason: str | None = None


def check_gates(stats: WalletStats, *, now: datetime) -> list[tuple[str, str]]:
    """Return failed gates as (kind, message). Empty = eligible.

    kind is "maturity" (insufficient history — rescan later) or
    "evidence" (actual negative signal).
    """
    failures: list[tuple[str, str]] = []
    if stats.settled_market_count < MIN_SETTLED_MARKETS:
        failures.append((
            "maturity",
            f"settled_markets {stats.settled_market_count} < {MIN_SETTLED_MARKETS}",
        ))
    if stats.trade_count < MIN_TRADES:
        failures.append(("maturity", f"trades {stats.trade_count} < {MIN_TRADES}"))
    if stats.first_trade_at is None:
        failures.append(("maturity", "no trade history"))
    else:
        age_days = (now - stats.first_trade_at).days
        if age_days < MIN_ACCOUNT_AGE_DAYS:
            failures.append((
                "maturity",
                f"account_age_days {age_days} < {MIN_ACCOUNT_AGE_DAYS}",
            ))
    if stats.last_trade_at is None:
        failures.append(("evidence", "no recent activity"))
    else:
        idle_days = (now - stats.last_trade_at).days
        if idle_days > MAX_INACTIVE_DAYS:
            failures.append((
                "evidence",
                f"inactive_days {idle_days} > {MAX_INACTIVE_DAYS}",
            ))
    # Non-positive P&L only counts as negative evidence once the history
    # gates are satisfied — judging P&L on 3 settled markets is noise.
    has_adequate_history = not any(
        kind == "maturity" for kind, _ in failures
    )
    if has_adequate_history and stats.realized_pnl <= 0:
        failures.append(("evidence", "realized_pnl not positive"))
    return failures


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _sample_strength(settled_market_count: int) -> float:
    """Evidence depth, size-neutral: 15 → 30, 30 → 60, 50+ → 100.

    Replaces absolute-dollar P&L as a component: bankroll size is not
    skill. Positive P&L remains an eligibility gate; this component
    rewards having enough resolved history to trust the other metrics.
    """
    return _clamp(100.0 * settled_market_count / _SAMPLE_STRENGTH_FULL)


def _concentration(per_market_pnl: list[Decimal]) -> tuple[float, Decimal]:
    """Score + the max P&L share. Share > 50% is a hard reject (checked by
    the caller). Only positive-P&L markets can dominate profit."""
    positives = [p for p in per_market_pnl if p > 0]
    total = sum(positives, Decimal(0))
    if not positives or total <= 0:
        return 0.0, Decimal(1)  # no profit → maximally concentrated
    share = max(positives) / total
    return _clamp(100.0 * (1.0 - float(share))), share


def _profit_factor(gross_profit: Decimal, gross_loss: Decimal) -> float:
    if gross_profit <= 0:
        return 0.0
    if gross_loss == 0:
        return 100.0  # no losses (with profit) → capped perfect
    pf = float(gross_profit / gross_loss)
    if pf <= 1.0:
        return 0.0
    return _clamp((pf - 1.0) / (_PROFIT_FACTOR_CAP - 1.0) * 100.0)


def _consistency(weekly_pnl: list[Decimal]) -> float:
    """% of active decision-weeks that were profitable.

    Measures consistency WHILE ACTIVE, not continuous weekly persistence —
    a wallet that trades three profitable weeks six weeks apart reads as
    3/3. The inactivity gate covers dormancy; see
    docs/wallet-intelligence.md §4.
    """
    active = [w for w in weekly_pnl if w != 0]
    if not active:
        return 0.0
    winners = sum(1 for w in active if w > 0)
    return _clamp(100.0 * winners / len(active))


def _recency(recent_30d_pnl: Decimal, lifetime_pnl: Decimal) -> float:
    """Fraction of lifetime realized P&L from DECISIONS made in the last
    30 days. A wallet whose edge died months ago scores near 0 even if
    its old positions only settled yesterday."""
    if lifetime_pnl <= 0:
        return 0.0
    return _clamp(100.0 * float(recent_30d_pnl / lifetime_pnl))


def score_wallet(stats: WalletStats, *, now: datetime | None = None) -> ScoreResult:
    """Run gates, then the five weighted components. Fully deterministic."""
    now = now or datetime.now(UTC)
    failures = check_gates(stats, now=now)
    if failures:
        messages = [msg for _, msg in failures]
        # Any maturity failure → insufficient history, NOT a judgment.
        if any(kind == "maturity" for kind, _ in failures):
            return ScoreResult(
                eligible=False,
                gate_failures=messages,
                verdict="insufficient_history",
            )
        return ScoreResult(
            eligible=False, gate_failures=messages, verdict="score_rejected"
        )

    sample = _sample_strength(stats.settled_market_count)
    conc, conc_share = _concentration(stats.per_market_pnl)
    pf = _profit_factor(stats.gross_profit, stats.gross_loss)
    cons = _consistency(stats.weekly_pnl)
    rec = _recency(stats.recent_30d_pnl, stats.realized_pnl)

    components = {
        "sample_strength": round(sample, 2),
        "concentration": round(conc, 2),
        "profit_factor": round(pf, 2),
        "consistency": round(cons, 2),
        "recency": round(rec, 2),
    }
    composite = round(sum(components[k] * WEIGHTS[k] for k in WEIGHTS), 2)

    result = ScoreResult(
        eligible=True,
        components=components,
        composite=composite,
    )
    if conc_share > CONCENTRATION_HARD_REJECT:
        result.verdict = "score_rejected"
        result.hard_reject_reason = (
            f"concentration {conc_share:.1%} > {CONCENTRATION_HARD_REJECT:.0%}"
        )
    elif composite >= VERDICT_REVIEW_THRESHOLD:
        result.verdict = "pending_review"
    elif composite >= VERDICT_REJECT_THRESHOLD:
        result.verdict = "discovered"
    else:
        result.verdict = "score_rejected"
    return result


def bucket_weekly(decided: list[tuple[datetime, Decimal]]) -> list[Decimal]:
    """(decision_ts, pnl) pairs → per-week totals. Empty weeks omitted."""
    weeks: dict[tuple[int, int], Decimal] = {}
    for decision_ts, pnl in decided:
        iso = decision_ts.isocalendar()
        key = (iso.year, iso.week)
        weeks[key] = weeks.get(key, Decimal(0)) + pnl
    return list(weeks.values())


def recent_pnl(
    decided: list[tuple[datetime, Decimal]], *, now: datetime, days: int = 30
) -> Decimal:
    cutoff = now - timedelta(days=days)
    return sum((p for ts, p in decided if ts >= cutoff), Decimal(0))
