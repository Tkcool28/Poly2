"""Decision-time CLOB taker fee curve for paper fills."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

FEE_SOURCE = "GET /clob-markets/{condition_id}"
FEE_QUANTUM = Decimal("0.00001")


@dataclass(frozen=True)
class MarketFee:
    rate: Decimal
    exponent: Decimal
    taker_only: bool

    @classmethod
    def from_market_info(cls, info: dict[str, Any]) -> MarketFee:
        if not isinstance(info, dict) or not isinstance(info.get("fd"), dict):
            raise TypeError("CLOB market info missing fd fee details")
        data = info["fd"]
        if not isinstance(data.get("to"), bool):
            raise TypeError("CLOB fee details missing taker-only status")
        try:
            # Reject booleans (bool is an int) and all non-numeric shapes.
            if any(isinstance(data.get(key), bool) for key in ("r", "e")):
                raise ValueError("invalid CLOB fee coefficient or exponent")
            rate = Decimal(str(data["r"]))
            exponent = Decimal(str(data["e"]))
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError("invalid CLOB fee coefficient or exponent") from exc
        if not rate.is_finite() or rate < 0 or not exponent.is_finite() or exponent < 0:
            raise ValueError("invalid CLOB fee coefficient or exponent")
        return cls(rate=rate, exponent=exponent, taker_only=data["to"])

    def taker_fee(self, shares: Decimal, price: Decimal) -> Decimal:
        if not (shares.is_finite() and shares > 0 and price.is_finite()
                and Decimal(0) < price <= Decimal(1)):
            raise ValueError("invalid paper fill for fee calculation")
        # Zero-rate markets stay exactly zero, including at price 1.
        if self.rate == 0:
            return Decimal("0.00000")
        return (shares * self.rate * price * (Decimal(1) - price) ** self.exponent).quantize(
            FEE_QUANTUM, rounding=ROUND_HALF_UP
        )
