"""Pure scoring logic — no DB, no network. Every number explainable.

The functions here consume a plain ``WalletStats`` snapshot (assembled
from accounting + trade history by ``scoring.service``) and produce a
``ScoreResult`` with a full component breakdown, so "why did the system
score this wallet 63?" is always answerable from the stored JSON.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

# --- Eligibility gates (docs/wallet-intelligence.md §3) -------------------
MIN_SETTLED_MARKETS = 15
MIN_TRADES = 30
MIN_ACCOUNT_AGE_DAYS = 30
MAX_INACTIVE_DAYS = 14
CONCENTRATION_HARD_REJECT = Decimal("0.50")

# --- Verdict thresholds (§4) ----------------------------------------------
VERDICT_REVIEW_THRESHOLD = 70.0
VERDICT_REJECT_THRESHOLD = 50.0

WEIGHTS = {
    "pnl_quality": 0.25,
    "concentration": 0.20,
    "profit_factor": 0.20,
    "consistency": 0.20,
    "recency": 0.15,
}

# P&L quality scale: log10-scaled, $10k realized → 100.
_PNL_FULL_SCORE_USD = Decimal(10_000)
# Profit factor cap: pf ≥ 5 → 100 (beyond that it's sample-size luck).
_PROFIT_FACTOR_CAP = 5.0


@dataclass
class WalletStats:
    """Everything V1 scoring needs, pre-assembled. Decimals, not floats."""

    realized_pnl: Decimal
    settled_market_count: int
    trade_count: int
    gross_profit: Decimal
    gross_loss: Decimal
    per_market_pnl: list[Decimal]  # realized P&L per settled market
    weekly_pnl: list[Decimal]  # realized P&L bucketed by settlement week
    recent_30d_pnl: Decimal  # realized P&L settled in the last 30 days
    first_trade_at: datetime | None
    last_trade_at: datetime | None


@dataclass
class ScoreResult:
    eligible: bool
    gate_failures: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    composite: float | None = None  # 0–100, None if ineligible
    verdict: str = "rejected"  # pending_review | discovered | rejected
    hard_reject_reason: str | None = None


def check_gates(stats: WalletStats, *, now: datetime) -> list[str]:
    """Return the list of failed gates (empty = eligible)."""
    failures: list[str] = []
    if stats.settled_market_count < MIN_SETTLED_MARKETS:
        failures.append(
            f"settled_markets {stats.settled_market_count} < {MIN_SETTLED_MARKETS}"
        )
    if stats.trade_count < MIN_TRADES:
        failures.append(f"trades {stats.trade_count} < {MIN_TRADES}")
    if stats.first_trade_at is None:
        failures.append("no trade history")
    else:
        age_days = (now - stats.first_trade_at).days
        if age_days < MIN_ACCOUNT_AGE_DAYS:
            failures.append(f"account_age_days {age_days} < {MIN_ACCOUNT_AGE_DAYS}")
    if stats.last_trade_at is None:
        failures.append("no recent activity")
    else:
        idle_days = (now - stats.last_trade_at).days
        if idle_days > MAX_INACTIVE_DAYS:
            failures.append(f"inactive_days {idle_days} > {MAX_INACTIVE_DAYS}")
    if stats.realized_pnl <= 0:
        failures.append("realized_pnl not positive")
    return failures


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _pnl_quality(pnl: Decimal) -> float:
    """Log-scaled realized P&L: $10 → ~25, $100 → 50, $1k → 75, $10k → 100."""
    if pnl <= 0:
        return 0.0
    return _clamp(
        100.0
        * math.log10(1 + float(pnl))
        / math.log10(1 + float(_PNL_FULL_SCORE_USD))
    )


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
    """% of active weeks that were profitable."""
    active = [w for w in weekly_pnl if w != 0]
    if not active:
        return 0.0
    winners = sum(1 for w in active if w > 0)
    return _clamp(100.0 * winners / len(active))


def _recency(recent_30d_pnl: Decimal, lifetime_pnl: Decimal) -> float:
    """Fraction of lifetime realized P&L earned in the last 30 days.

    A wallet whose edge died months ago scores near 0; one still earning
    scores high. Requires positive lifetime P&L (gate guarantees it).
    """
    if lifetime_pnl <= 0:
        return 0.0
    return _clamp(100.0 * float(recent_30d_pnl / lifetime_pnl))


def score_wallet(stats: WalletStats, *, now: datetime | None = None) -> ScoreResult:
    """Run gates, then the five weighted components. Fully deterministic."""
    now = now or datetime.now(UTC)
    failures = check_gates(stats, now=now)
    if failures:
        return ScoreResult(eligible=False, gate_failures=failures, verdict="rejected")

    pnl_q = _pnl_quality(stats.realized_pnl)
    conc, conc_share = _concentration(stats.per_market_pnl)
    pf = _profit_factor(stats.gross_profit, stats.gross_loss)
    cons = _consistency(stats.weekly_pnl)
    rec = _recency(stats.recent_30d_pnl, stats.realized_pnl)

    components = {
        "pnl_quality": round(pnl_q, 2),
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
        result.verdict = "rejected"
        result.hard_reject_reason = (
            f"concentration {conc_share:.1%} > {CONCENTRATION_HARD_REJECT:.0%}"
        )
    elif composite >= VERDICT_REVIEW_THRESHOLD:
        result.verdict = "pending_review"
    elif composite >= VERDICT_REJECT_THRESHOLD:
        result.verdict = "discovered"
    else:
        result.verdict = "rejected"
    return result


def bucket_weekly(settled: list[tuple[datetime, Decimal]]) -> list[Decimal]:
    """(settled_at, pnl) pairs → per-week totals. Empty weeks omitted."""
    weeks: dict[tuple[int, int], Decimal] = {}
    for settled_at, pnl in settled:
        iso = settled_at.isocalendar()
        key = (iso.year, iso.week)
        weeks[key] = weeks.get(key, Decimal(0)) + pnl
    return list(weeks.values())


def recent_pnl(
    settled: list[tuple[datetime, Decimal]], *, now: datetime, days: int = 30
) -> Decimal:
    cutoff = now - timedelta(days=days)
    return sum((p for ts, p in settled if ts >= cutoff), Decimal(0))
