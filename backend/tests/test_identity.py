"""Canonical trade identity — the contract, enforced in code."""

from __future__ import annotations

import pytest

from polycopy.ingestion.identity import canonical_trade_id, normalize_address


def _trade(**over):
    base = {
        "transactionHash": "0xabc123",
        "proxyWallet": "0xC69BD5E6F0EXAMPLE",
        "asset": "466721272297",
        "size": 5,
        "price": 0.002,
        "timestamp": 1789795243,
    }
    base.update(over)
    return base


def test_key_format_and_wallet_normalization():
    key = canonical_trade_id(_trade())
    assert key == "data-api:0xabc123:0xc69bd5e6f0example:466721272297:5:0.002:1789795243"


def test_numeric_normalization_collapses_equivalent_forms():
    assert canonical_trade_id(_trade(size=5)) == canonical_trade_id(_trade(size=5.0))
    assert canonical_trade_id(_trade(price=0.002)) == canonical_trade_id(
        _trade(price=0.0020)
    )


def test_distinct_fields_change_key():
    base = canonical_trade_id(_trade())
    assert canonical_trade_id(_trade(timestamp=1789795244)) != base
    assert canonical_trade_id(_trade(transactionHash="0xdef456")) != base
    assert canonical_trade_id(_trade(size=6)) != base


def test_outcome_index_is_never_part_of_key():
    """outcomeIndex observed as 0/1/999 for identical trades — banned."""
    assert canonical_trade_id(_trade(outcomeIndex=0)) == canonical_trade_id(
        _trade(outcomeIndex=999)
    )


def test_missing_identity_field_raises():
    with pytest.raises(ValueError, match="transactionHash"):
        canonical_trade_id(_trade(transactionHash=None))


def test_normalize_address():
    assert normalize_address("  0xABCDEF ") == "0xabcdef"
