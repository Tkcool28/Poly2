"""Order-book walking: the pure math of realistic paper fills.

Given a side, a size, and the detection-time book, walk level by level and
return exactly what a market order would have gotten — full, partial, or
nothing. Decimal-only: this feeds cash accounting downstream.

This module is deliberately I/O-free so every behavior is unit-testable
without mocking HTTP. The evidence rules live in
docs/paper-execution-model.md:

* fill price is the volume-weighted price of the levels consumed,
  NEVER the source wallet's price;
* partial fill when book depth < our size is a real outcome;
* no fill (missed) when there is no depth at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class FillResult:
    """What walking the book produced for our order."""

    status: str  # "filled" | "partial" | "missed"
    filled_size: Decimal
    fill_price: Decimal | None  # volume-weighted; None when missed
    # Total shares available on OUR side of the book (evidence for sizing
    # decisions) — NOT what we consumed. What we consumed is filled_size.
    depth_available: Decimal
    levels_consumed: int


def walk_book(
    side: str,
    size_usd: Decimal,
    bids: list[BookLevel],
    asks: list[BookLevel],
    *,
    fee_rate: Decimal = Decimal(0),
    max_shares: Decimal | None = None,
) -> tuple[FillResult, Decimal]:
    """Simulate a market order of ``size_usd`` notional against the book.

    BUY consumes asks (best = lowest price first); SELL consumes bids
    (best = highest first). Returns (FillResult, fee). Fee is charged on
    filled notional; zero when nothing fills.

    ``max_shares`` caps the shares taken (e.g. never sell more than the
    paper position owns). The cap binds before the notional target.
    """
    if size_usd <= 0:
        raise ValueError("size_usd must be positive")
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")

    levels = sorted(
        asks if side == "BUY" else bids,
        key=lambda lv: lv.price,
        reverse=side == "SELL",
    )
    depth = sum((lv.size for lv in levels), Decimal(0))
    if not levels or depth <= 0:
        return (
            FillResult("missed", Decimal(0), None, Decimal(0), 0),
            Decimal(0),
        )

    remaining_usd = size_usd
    remaining_shares = max_shares  # None = uncapped
    shares = Decimal(0)
    notional = Decimal(0)  # sum(take × level price) — exact per level
    used = 0
    for lv in levels:
        if remaining_usd <= 0:
            break
        if remaining_shares is not None and remaining_shares <= 0:
            break
        # Shares we can take at this level: limited by level depth, by the
        # notional remaining, and by the optional share cap.
        affordable = remaining_usd / lv.price
        take = min(lv.size, affordable)
        if remaining_shares is not None:
            take = min(take, remaining_shares)
        notional_bound = take == affordable  # money ran out at this level
        if remaining_shares is not None:
            remaining_shares -= take
        shares += take
        notional += take * lv.price
        used += 1
        if notional_bound:
            # Exact zero — inexact division would otherwise leave a dust
            # residue and misreport a complete fill as partial.
            remaining_usd = Decimal(0)
            break
        remaining_usd -= take * lv.price

    spent = size_usd - remaining_usd  # actual notional filled
    if shares <= 0:
        return (
            FillResult("missed", Decimal(0), None, depth, 0),
            Decimal(0),
        )

    vwap = notional / shares  # exact per-level prices, no division dust
    # Filled = notional target met. If the share cap (or depth) bound first,
    # the target wasn't met → partial. filled_size and the position change
    # always agree because the cap is applied INSIDE the walk.
    status = "filled" if remaining_usd <= 0 else "partial"
    fee = (spent * fee_rate).quantize(Decimal("0.000001"))
    return (
        FillResult(status, shares, vwap, depth, used),
        fee,
    )
