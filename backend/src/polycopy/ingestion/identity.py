"""Canonical identity helpers — the single implementation of the contract.

Every dedup / idempotency decision in the system goes through
``canonical_trade_id``. See docs/source-identity-contract.md for the audit
evidence behind these rules:

* The Data API exposes NO trade-level unique ID.
* ``transactionHash`` alone is insufficient (one tx, multiple fills).
* ``(wallet, asset, size, price, timestamp)`` alone is ALSO insufficient —
  real distinct trades have been observed sharing all five fields in the
  same second.
* ``outcomeIndex`` is unreliable (observed 0/1/999 for identical trades)
  and is NEVER part of the key.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def normalize_address(address: str) -> str:
    """Wallet identity is the lowercase proxyWallet address."""
    return address.strip().lower()


def _num(value: Any) -> str:
    """Canonical string form for numeric key components.

    Uses Decimal to avoid float repr drift (0.1 -> '0.1', not
    '0.1000000000000000055511151231257827'). The Data API sends JSON
    numbers, which Python parses as float; Decimal(str(x)) round-trips
    them faithfully.
    """
    d = Decimal(str(value))
    # Normalize so 5, 5.0, and 5.00 all key identically.
    return format(d.normalize(), "f")


def canonical_trade_id(trade: dict[str, Any]) -> str:
    """Composite canonical key for one Data API trade object.

    Format: data-api:{txHash}:{proxyWallet}:{asset}:{size}:{price}:{timestamp}
    Max realistic length fits String(160) (migration 0002).
    """
    required = ("transactionHash", "proxyWallet", "asset", "size", "price", "timestamp")
    missing = [k for k in required if trade.get(k) is None]
    if missing:
        raise ValueError(f"trade object missing identity fields: {missing}")
    return ":".join(
        [
            "data-api",
            str(trade["transactionHash"]),
            normalize_address(str(trade["proxyWallet"])),
            str(trade["asset"]),
            _num(trade["size"]),
            _num(trade["price"]),
            str(trade["timestamp"]),
        ]
    )
