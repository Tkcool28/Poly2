"""Pure scoring V1: gates, components, verdicts — docs/wallet-intelligence.md.

Revised after review: maturity gates → insufficient_history (not a
rejection); absolute P&L is NOT a component; recency uses decision time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polycopy.scoring import score as sc

NOW = datetime(2026, 9, 20, tzinfo=UTC)


def _stats(**over) -> sc.WalletStats:
    """A fully eligible, decent wallet; tests override one thing at a time."""
    base = {
        "realized_pnl": Decimal(1000),
        "settled_market_count": 50,
        "trade_count": 120,
        "gross_profit": Decimal(1500),
        "gross_loss": Decimal(500),
        "per_market_pnl": [Decimal(20)] * 50,  # perfectly diversified
        "weekly_pnl": [Decimal(100)] * 8 + [Decimal(-20)] * 2,
        "recent_30d_pnl": Decimal(500),
        "first_trade_at": NOW - timedelta(days=90),
        "last_trade_at": NOW - timedelta(days=1),
    }
    base.update(over)
    return sc.WalletStats(**base)


# --- Gates -------------------------------------------------------------------


def test_eligible_wallet_passes_all_gates():
    assert sc.check_gates(_stats(), now=NOW) == []


def test_gate_kinds_distinguish_maturity_from_evidence():
    failures = {
        msg.split()[0]: kind
        for kind, msg in sc.check_gates(
            _stats(
                settled_market_count=14,
                trade_count=29,
                last_trade_at=NOW - timedelta(days=30),
            ),
            now=NOW,
        )
    }
    assert failures["settled_markets"] == "maturity"
    assert failures["trades"] == "maturity"
    assert failures["inactive_days"] == "evidence"


def test_maturity_failure_is_insufficient_history_not_rejection():
    """14 settled markets + 29 trades + active + profitable: NOT bad,
    just not enough evidence yet. Must NOT be a rejection verdict."""
    result = sc.score_wallet(
        _stats(settled_market_count=14, trade_count=29), now=NOW
    )
    assert result.eligible is False
    assert result.verdict == "insufficient_history"
    assert result.composite is None


def test_young_account_is_insufficient_history():
    result = sc.score_wallet(
        _stats(first_trade_at=NOW - timedelta(days=10)), now=NOW
    )
    assert result.verdict == "insufficient_history"


def test_negative_pnl_with_adequate_history_is_score_rejected():
    result = sc.score_wallet(_stats(realized_pnl=Decimal(-5)), now=NOW)
    assert result.verdict == "score_rejected"


def test_negative_pnl_with_immature_history_is_not_negative_evidence():
    """PnL judgment on 3 settled markets is noise — maturity dominates."""
    result = sc.score_wallet(
        _stats(realized_pnl=Decimal(-5), settled_market_count=3, trade_count=4),
        now=NOW,
    )
    assert result.verdict == "insufficient_history"


def test_dormant_wallet_is_score_rejected():
    result = sc.score_wallet(
        _stats(last_trade_at=NOW - timedelta(days=30)), now=NOW
    )
    assert result.verdict == "score_rejected"
    assert any("inactive" in f for f in result.gate_failures)


# --- Components ----------------------------------------------------------------


def test_sample_strength_is_size_neutral():
    """Evidence depth, not dollars: 15 → 30, 30 → 60, 50+ → 100."""
    assert sc._sample_strength(15) == 30.0
    assert sc._sample_strength(30) == 60.0
    assert sc._sample_strength(50) == 100.0
    assert sc._sample_strength(500) == 100.0


def test_absolute_pnl_is_not_a_component():
    """$100 and $100,000 of realized P&L must score identically when all
    other evidence is equal — bankroll size is not skill."""
    # recent_30d_pnl scaled proportionally (50%) so only the absolute
    # dollar amounts differ — components must be identical.
    small = sc.score_wallet(
        _stats(realized_pnl=Decimal(100), recent_30d_pnl=Decimal(50)), now=NOW
    )
    big = sc.score_wallet(
        _stats(realized_pnl=Decimal(100_000), recent_30d_pnl=Decimal(50_000)),
        now=NOW,
    )
    assert small.components == big.components
    assert small.composite == big.composite


def test_concentration_hard_reject_overrides_high_score():
    result = sc.score_wallet(
        _stats(per_market_pnl=[Decimal(950), Decimal(50)]), now=NOW
    )
    assert result.verdict == "score_rejected"
    assert "concentration" in result.hard_reject_reason


def test_diversified_profit_scores_high_concentration_component():
    conc, share = sc._concentration([Decimal(50)] * 20)
    assert share == Decimal("0.05")
    assert conc == 95.0


def test_profit_factor_scaling():
    assert sc._profit_factor(Decimal(100), Decimal(100)) == 0.0
    assert sc._profit_factor(Decimal(500), Decimal(100)) == 100.0
    assert sc._profit_factor(Decimal(100), Decimal(0)) == 100.0
    assert sc._profit_factor(Decimal(0), Decimal(100)) == 0.0


def test_consistency_counts_profitable_active_weeks():
    assert sc._consistency([Decimal(10)] * 10) == 100.0
    assert sc._consistency([Decimal(10)] * 5 + [Decimal(-10)] * 5) == 50.0
    assert sc._consistency([]) == 0.0


def test_recency_rewards_recent_decisions():
    assert sc._recency(Decimal(500), Decimal(1000)) == 50.0
    assert sc._recency(Decimal(0), Decimal(1000)) == 0.0
    assert sc._recency(Decimal(1200), Decimal(1000)) == 100.0  # clamps


# --- Verdicts ------------------------------------------------------------------


def test_strong_wallet_goes_to_pending_review():
    result = sc.score_wallet(_stats(), now=NOW)
    assert result.verdict == "pending_review"
    assert result.composite >= 70


def test_mediocre_wallet_stays_discovered():
    result = sc.score_wallet(
        _stats(
            settled_market_count=16,
            gross_profit=Decimal(400),
            gross_loss=Decimal(200),
            weekly_pnl=[Decimal(10)] * 4 + [Decimal(-10)] * 2,
            recent_30d_pnl=Decimal(400),
        ),
        now=NOW,
    )
    assert result.eligible is True
    assert result.verdict == "discovered"
    assert 50 <= result.composite < 70


def test_weak_wallet_score_rejected_below_50():
    result = sc.score_wallet(
        _stats(
            settled_market_count=15,
            gross_profit=Decimal(2),
            gross_loss=Decimal(1),
            weekly_pnl=[Decimal(-1)] * 9 + [Decimal(2)],
            recent_30d_pnl=Decimal(0),
        ),
        now=NOW,
    )
    assert result.verdict == "score_rejected"
    assert result.composite < 50


# --- Helpers -------------------------------------------------------------------


def test_bucket_weekly_groups_by_decision_week():
    wk = sc.bucket_weekly(
        [
            (datetime(2026, 9, 14, tzinfo=UTC), Decimal(10)),
            (datetime(2026, 9, 15, tzinfo=UTC), Decimal(5)),
            (datetime(2026, 9, 21, tzinfo=UTC), Decimal(-3)),
        ]
    )
    assert sorted(wk) == [Decimal(-3), Decimal(15)]


def test_recent_pnl_uses_decision_time():
    """GPT review case: a decision made 6 months ago that SETTLED yesterday
    must NOT count as recent. The pairs carry decision timestamps."""
    decided = [
        (NOW - timedelta(days=180), Decimal(500)),  # old decision
        (NOW - timedelta(days=5), Decimal(10)),  # recent decision
    ]
    assert sc.recent_pnl(decided, now=NOW) == Decimal(10)
