"""CLOB market-specific taker fees, isolated from book pricing."""

from decimal import Decimal

import pytest

from polycopy.execution.bookwalk import BookLevel, walk_book
from polycopy.execution.fees import MarketFee

D = Decimal


def curve(r: str, e: str = "1") -> MarketFee:
    return MarketFee.from_market_info({"fd": {"r": r, "e": e, "to": True}})


def test_fee_free_and_standard_fee_enabled() -> None:
    assert curve("0").taker_fee(D("20"), D("0.5")) == D("0.00000")
    assert curve("0.05").taker_fee(D("20"), D("0.5")) == D("0.25000")


def test_nonlinear_exponent_and_five_place_rounding() -> None:
    assert curve("0.05", "2").taker_fee(D("20"), D("0.5")) == D("0.06250")
    assert curve("0.05", "2").taker_fee(D("20"), D("0.3")) == D("0.04410")
    assert curve("0.05", "2").taker_fee(D("20"), D("0.7")) == D("0.04410")
    assert curve("0.01").taker_fee(D("1"), D("0.3333")) == D("0.00222")
    assert curve("0.00001").taker_fee(D("1"), D("0.5")) == D("0.00000")
    assert curve("0.00002").taker_fee(D("1"), D("0.5")) == D("0.00001")


@pytest.mark.parametrize("info,state", [
    ({}, "absent"), ({"fd": None}, "null"), ({"fd": {}}, "empty"),
])
def test_fee_free_missing_fee_block(info: dict, state: str) -> None:
    fee = MarketFee.from_market_info(info)
    assert fee.rate == fee.exponent == 0
    assert fee.taker_only is None
    assert fee.metadata_state == state
    assert fee.taker_fee(D("20"), D("0.5")) == D("0.00000")


def test_role_flag_is_optional_when_curve_is_valid() -> None:
    fee = MarketFee.from_market_info({"fd": {"r": "0.05", "e": 2}})
    assert fee.taker_only is None
    assert fee.taker_fee(D("20"), D("0.5")) == D("0.06250")


def test_partial_and_multi_level_fee_use_actual_fill_vwap() -> None:
    fee_curve = curve("0.05")
    partial = walk_book("BUY", D("10"), [], [BookLevel(D("0.5"), D("10"))])
    assert partial.status == "partial"
    assert fee_curve.taker_fee(partial.filled_size, partial.fill_price) == D("0.12500")
    levels = [BookLevel(D("0.4"), D("10")), BookLevel(D("0.5"), D("100"))]
    multi = walk_book("BUY", D("10"), [], levels)
    assert multi.filled_size == 22
    assert multi.fill_price == D("10") / D("22")
    assert fee_curve.taker_fee(multi.filled_size, multi.fill_price) == D("0.27273")


@pytest.mark.parametrize("fd", [
    {"r": "0.05"}, {"r": "nan", "e": 1, "to": True},
    {"r": -1, "e": 1, "to": True}, {"r": 0, "e": "inf", "to": True},
    {"r": True, "e": 1, "to": True}, {"r": 0, "e": 1, "to": "true"},
])
def test_malformed_market_fee_is_rejected(fd: dict | None) -> None:
    with pytest.raises((TypeError, ValueError)):
        MarketFee.from_market_info({"fd": fd})
