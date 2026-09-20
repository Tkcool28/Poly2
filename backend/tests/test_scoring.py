"""Pure scoring V1: gates, components, verdicts — docs/wallet-intelligence.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from polycopy.scoring import score as sc

NOW = datetime(2026, 9, 20, tzinfo=UTC)


def _stats(**over) -> sc.WalletStats:
    """A fully eligible, decent wallet; tests override one thing at a time."""
    base = {
        "realized_pnl": Decimal(1000),
        "settled_market_count": 20,
        "trade_count": 50,
        "gross_profit": Decimal(1500),
        "gross_loss": Decimal(500),
        "per_market_pnl": [Decimal(50)] * 20,  # perfectly diversified
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


def test_each_gate_fails_independently():
    cases = [
        ({"settled_market_count": 14}, "settled_markets"),
        ({"trade_count": 29}, "trades"),
        ({"first_trade_at": NOW - timedelta(days=10)}, "account_age"),
        ({"last_trade_at": NOW - timedelta(days=30)}, "inactive"),
        ({"realized_pnl": Decimal(-5)}, "realized_pnl"),
        ({"realized_pnl": Decimal(0)}, "realized_pnl"),
    ]
    for over, needle in cases:
        failures = sc.check_gates(_stats(**over), now=NOW)
        assert any(needle in f for f in failures), (over, failures)


def test_gate_failure_means_rejected_no_composite():
    result = sc.score_wallet(_stats(settled_market_count=2), now=NOW)
    assert result.eligible is False
    assert result.composite is None
    assert result.verdict == "rejected"


# --- Components ----------------------------------------------------------------


def test_pnl_quality_log_scale():
    assert sc._pnl_quality(Decimal(0)) == 0.0
    assert sc._pnl_quality(Decimal(100)) == pytest.approx(50.0, abs=0.5)
    assert sc._pnl_quality(Decimal(10000)) == pytest.approx(100.0, abs=0.01)
    assert 25 < sc._pnl_quality(Decimal(10)) < 27


def test_concentration_hard_reject_overrides_high_score():
    # One market made ALL the profit → hard reject even with great numbers.
    result = sc.score_wallet(
        _stats(per_market_pnl=[Decimal(950), Decimal(50)]), now=NOW
    )
    assert result.verdict == "rejected"
    assert "concentration" in result.hard_reject_reason


def test_diversified_profit_scores_high_concentration_component():
    conc, share = sc._concentration([Decimal(50)] * 20)
    assert share == Decimal("0.05")
    assert conc == 95.0


def test_profit_factor_scaling():
    assert sc._profit_factor(Decimal(100), Decimal(100)) == 0.0  # pf 1
    assert sc._profit_factor(Decimal(500), Decimal(100)) == 100.0  # pf 5+
    assert sc._profit_factor(Decimal(100), Decimal(0)) == 100.0  # no losses
    assert sc._profit_factor(Decimal(0), Decimal(100)) == 0.0


def test_consistency_counts_profitable_weeks():
    assert sc._consistency([Decimal(10)] * 10) == 100.0
    assert sc._consistency([Decimal(10)] * 5 + [Decimal(-10)] * 5) == 50.0
    assert sc._consistency([]) == 0.0


def test_recency_rewards_recent_profit():
    assert sc._recency(Decimal(500), Decimal(1000)) == 50.0
    assert sc._recency(Decimal(0), Decimal(1000)) == 0.0
    # Recent profit exceeding lifetime (old losses) clamps at 100
    assert sc._recency(Decimal(1200), Decimal(1000)) == 100.0


# --- Verdicts ------------------------------------------------------------------


def test_strong_wallet_goes_to_pending_review():
    result = sc.score_wallet(_stats(), now=NOW)
    assert result.verdict == "pending_review"
    assert result.composite >= 70


def test_mediocre_wallet_stays_discovered():
    # Eligible but middling: small pnl, pf ~1.7, half weeks profitable,
    # little recent profit.
    result = sc.score_wallet(
        _stats(
            realized_pnl=Decimal(200),
            gross_profit=Decimal(300),
            gross_loss=Decimal(100),
            weekly_pnl=[Decimal(10)] * 6 + [Decimal(-10)] * 2,
            recent_30d_pnl=Decimal(60),
        ),
        now=NOW,
    )
    assert result.eligible is True
    assert result.verdict == "discovered"
    assert 50 <= result.composite < 70


def test_weak_wallet_rejected_below_50():
    result = sc.score_wallet(
        _stats(
            realized_pnl=Decimal(1),
            gross_profit=Decimal(2),
            gross_loss=Decimal(1),
            weekly_pnl=[Decimal(-1)] * 9 + [Decimal(2)],
            recent_30d_pnl=Decimal(0),
        ),
        now=NOW,
    )
    assert result.verdict == "rejected"
    assert result.composite < 50


# --- Helpers -------------------------------------------------------------------


def test_bucket_weekly_groups_by_iso_week():
    wk = sc.bucket_weekly(
        [
            (datetime(2026, 9, 14, tzinfo=UTC), Decimal(10)),
            (datetime(2026, 9, 15, tzinfo=UTC), Decimal(5)),
            (datetime(2026, 9, 21, tzinfo=UTC), Decimal(-3)),
        ]
    )
    assert sorted(wk) == [Decimal(-3), Decimal(15)]


def test_recent_pnl_window():
    settled = [
        (NOW - timedelta(days=5), Decimal(10)),
        (NOW - timedelta(days=40), Decimal(100)),
    ]
    assert sc.recent_pnl(settled, now=NOW) == Decimal(10)
