"""Book-walk math: full / partial / missed fills, VWAP, fees. Pure Decimal."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.execution.bookwalk import BookLevel, walk_book

D = Decimal


def _levels(*pairs: tuple[str, str]) -> list[BookLevel]:
    return [BookLevel(price=D(p), size=D(s)) for p, s in pairs]


def test_full_fill_single_level():
    asks = _levels(("0.50", "100"))
    result, fee = walk_book("BUY", D("10"), [], asks)
    assert result.status == "filled"
    assert result.filled_size == D("20")
    assert result.fill_price == D("0.5")
    assert result.levels_consumed == 1
    assert fee == 0


def test_full_fill_walks_multiple_levels_vwap():
    # $10: 10 shares @ 0.40 ($4) + 12 shares @ 0.50 ($6) → vwap 10/22
    asks = _levels(("0.50", "100"), ("0.40", "10"))
    result, _ = walk_book("BUY", D("10"), [], asks)
    assert result.status == "filled"
    assert result.filled_size == D("22")
    assert result.fill_price == D("10") / D("22")
    assert result.levels_consumed == 2
    # Slippage vs top-of-book is real: vwap > 0.40
    assert result.fill_price > D("0.40")


def test_sell_walks_bids_highest_first():
    bids = _levels(("0.40", "10"), ("0.55", "100"))
    result, _ = walk_book("SELL", D("11"), bids, [])
    assert result.status == "filled"
    assert result.filled_size == D("20")  # all at 0.55 — best bid first
    assert result.fill_price == D("0.55")


def test_partial_fill_when_depth_runs_out():
    asks = _levels(("0.50", "10"))  # only $5 of depth
    result, fee = walk_book("BUY", D("10"), [], asks, fee_rate=D("0.01"))
    assert result.status == "partial"
    assert result.filled_size == D("10")
    assert result.fill_price == D("0.5")
    assert result.depth_available == D("10")
    assert fee == D("5") * D("0.01")  # fee on filled notional only


def test_missed_when_no_depth():
    result, fee = walk_book("BUY", D("10"), [], [])
    assert result.status == "missed"
    assert result.filled_size == 0
    assert result.fill_price is None
    assert fee == 0


def test_missed_when_zero_size_levels():
    asks = _levels(("0.50", "0"))
    result, _ = walk_book("BUY", D("10"), [], asks)
    assert result.status == "missed"


def test_fee_rate_zero_is_no_op():
    asks = _levels(("0.50", "100"))
    _, fee = walk_book("BUY", D("10"), [], asks, fee_rate=D("0"))
    assert fee == 0


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        walk_book("BUY", D("0"), [], _levels(("0.5", "1")))
    with pytest.raises(ValueError):
        walk_book("HOLD", D("1"), [], [])


def test_max_shares_caps_the_walk():
    # $10 target but only 5 shares allowed: cap binds inside the walk.
    result, fee = walk_book("SELL", D("10"), _levels(("0.50", "100")), [],
                            max_shares=D("5"))
    assert result.status == "partial"  # $10 target not met
    assert result.filled_size == D("5")
    assert result.fill_price == D("0.5")
    assert fee == 0  # zero fee rate default


def test_max_shares_multi_level_vwap_is_capped_shares_only():
    # Cap 15 across two bid levels: 10 @ 0.55 + 5 @ 0.40 → vwap on 15 only.
    bids = _levels(("0.40", "100"), ("0.55", "10"))
    result, _ = walk_book("SELL", D("50"), bids, [], max_shares=D("15"))
    assert result.filled_size == D("15")
    assert result.fill_price == (D("10") * D("0.55") + D("5") * D("0.40")) / D("15")


def test_max_shares_none_is_uncapped():
    asks = _levels(("0.50", "100"))
    result, _ = walk_book("BUY", D("10"), [], asks, max_shares=None)
    assert result.filled_size == D("20")
    assert result.status == "filled"
