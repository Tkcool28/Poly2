"""Pure cash-flow P&L math — the numbers scoring will trust."""

from __future__ import annotations

from decimal import Decimal

from polycopy.accounting.pnl import TradeLeg, account_market, summarize


def leg(outcome="Up", side="BUY", size="10", price="0.60", market="0xm1"):
    return TradeLeg(
        market_key=market,
        outcome=outcome,
        side=side,
        size=Decimal(size),
        price=Decimal(price),
    )


def test_winning_buy_pays_one_dollar_per_share():
    # Buy 10 Up @ 0.60 → cost 6.00; Up wins → payout 10 → P&L +4.00
    m = account_market("0xm1", [leg()], winning_outcome="Up")
    assert m.resolved is True
    assert m.payout == Decimal(10)
    assert m.realized_pnl == Decimal("4.00")


def test_losing_buy_is_a_full_loss_of_stake():
    m = account_market("0xm1", [leg()], winning_outcome="Down")
    assert m.payout == Decimal(0)
    assert m.realized_pnl == Decimal("-6.00")


def test_selling_before_resolution_locks_cash():
    # Buy 10 @ 0.60, sell 10 @ 0.80 → +2.00 regardless of outcome
    legs = [leg(), leg(side="SELL", price="0.80")]
    for winner in ("Up", "Down"):
        m = account_market("0xm1", legs, winning_outcome=winner)
        assert m.realized_pnl == Decimal("2.00"), winner


def test_partial_sell_plus_payout():
    # Buy 10 @ 0.50 (−5), sell 4 @ 0.70 (+2.80), 6 shares remain, win → +6
    # P&L = −5 + 2.80 + 6 = +3.80
    legs = [leg(price="0.50"), leg(side="SELL", size="4", price="0.70")]
    m = account_market("0xm1", legs, winning_outcome="Up")
    assert m.realized_pnl == Decimal("3.80")


def test_two_outcome_positions_both_counted():
    # Buy 10 Up @ 0.60 (−6) and 4 Down @ 0.30 (−1.20); Up wins → +10
    # P&L = −7.20 + 10 = +2.80
    legs = [leg(), leg(outcome="Down", size="4", price="0.30")]
    m = account_market("0xm1", legs, winning_outcome="Up")
    assert m.realized_pnl == Decimal("2.80")


def test_unresolved_market_has_no_realized_pnl():
    m = account_market("0xm1", [leg()], winning_outcome=None)
    assert m.resolved is False
    assert m.realized_pnl is None
    assert m.positions["Up"].shares == Decimal(10)


def test_summarize_counts_only_resolved_pnl():
    won = account_market("0xm1", [leg()], winning_outcome="Up")  # +4
    lost = account_market("0xm2", [leg(market="0xm2", price="0.40")],
                          winning_outcome="Down")  # −4 (10 @ 0.40 = 4)
    open_m = account_market("0xm3", [leg(market="0xm3")], winning_outcome=None)
    s = summarize([won, lost, open_m])
    assert s["realized_pnl"] == Decimal("0.00")
    assert s["settled_market_count"] == 2
    assert s["open_market_count"] == 1
    assert s["trade_count"] == 3
    assert s["winning_market_count"] == 1
    assert s["gross_profit"] == Decimal("4.00")
    assert s["gross_loss"] == Decimal("4.00")


def test_no_float_drift_in_money_math():
    # 0.1 + 0.2 != 0.3 in floats; Decimal must keep it exact.
    legs = [leg(size="1", price="0.1"), leg(size="1", price="0.2")]
    m = account_market("0xm1", legs, winning_outcome="Up")
    # cost 0.3, payout 2 → +1.7 exactly
    assert m.realized_pnl == Decimal("1.7")
