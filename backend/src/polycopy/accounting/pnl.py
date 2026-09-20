"""Pure cash-flow P&L math — no DB, no network, fully deterministic.

Method (deliberately the simplest correct thing):

* BUY  ``size`` shares at ``price`` → cash flow ``-size * price - fee``
* SELL ``size`` shares at ``price`` → cash flow ``+size * price - fee``
* At settlement, each share of the winning outcome still held pays $1.

``fee`` is the USDC fee charged on that fill (``trades.fee``). It is
subtracted from cash flow on BOTH sides — a fee is money out, always.

Realized P&L is only ever computed for RESOLVED markets. Unresolved
markets are reported as open positions (share counts), never as P&L —
guessing unrealized value is how accounting quietly lies.

All money math uses Decimal; floats never touch a total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class TradeLeg:
    """One executed trade, already normalized from the ingestion layer."""

    market_key: str  # markets.condition_id
    outcome: str  # display outcome ("Up"/"Down") — position identity
    side: str  # "BUY" | "SELL"
    size: Decimal
    price: Decimal
    # USDC fee charged on this fill. The Data API /trades response carries
    # no fee field (probe-audited field list, 2026-09-19), so ingested
    # trades have fee=0 today — but accounting must subtract it the moment
    # fees exist, or realized P&L silently overstates.
    fee: Decimal = Decimal(0)


@dataclass
class MarketPosition:
    """Running position in one outcome of one market."""

    shares: Decimal = Decimal(0)  # net shares held (buys - sells)
    cash_flow: Decimal = Decimal(0)  # sum of -buy / +sell amounts


@dataclass
class MarketAccounting:
    """Accounting result for one market."""

    market_key: str
    resolved: bool
    winning_outcome: str | None = None
    realized_pnl: Decimal | None = None  # only set when resolved
    payout: Decimal = Decimal(0)
    positions: dict[str, MarketPosition] = field(default_factory=dict)
    trade_count: int = 0


def account_market(
    market_key: str,
    legs: list[TradeLeg],
    *,
    winning_outcome: str | None,
) -> MarketAccounting:
    """Cash-flow accounting for one market's trade legs.

    ``winning_outcome=None`` means unresolved → no realized P&L.
    Selling short (net negative shares) is treated as invalid input for
    copy-trading purposes: payout is computed on max(shares, 0), and the
    cash flows still count, so P&L stays honest.
    """
    result = MarketAccounting(
        market_key=market_key,
        resolved=winning_outcome is not None,
        winning_outcome=winning_outcome,
        trade_count=len(legs),
    )
    for leg in legs:
        pos = result.positions.setdefault(leg.outcome, MarketPosition())
        signed = leg.size if leg.side == "BUY" else -leg.size
        pos.shares += signed
        gross = -leg.size * leg.price if leg.side == "BUY" else leg.size * leg.price
        pos.cash_flow += gross - leg.fee  # fees are always money out

    if winning_outcome is not None:
        win_pos = result.positions.get(winning_outcome)
        if win_pos is not None and win_pos.shares > 0:
            result.payout = win_pos.shares  # $1 per winning share
        total_cash = sum((p.cash_flow for p in result.positions.values()), Decimal(0))
        result.realized_pnl = total_cash + result.payout
    return result


def summarize(markets: list[MarketAccounting]) -> dict[str, Decimal | int]:
    """Wallet-level rollup. Only resolved markets contribute P&L."""
    resolved = [m for m in markets if m.resolved]
    total_pnl = sum((m.realized_pnl or Decimal(0) for m in resolved), Decimal(0))
    winners = sum(1 for m in resolved if (m.realized_pnl or 0) > 0)
    gross_profit = sum(
        (m.realized_pnl for m in resolved if (m.realized_pnl or 0) > 0), Decimal(0)
    )
    gross_loss = -sum(
        (m.realized_pnl for m in resolved if (m.realized_pnl or 0) < 0), Decimal(0)
    )
    return {
        "realized_pnl": total_pnl,
        "settled_market_count": len(resolved),
        "open_market_count": len(markets) - len(resolved),
        "trade_count": sum(m.trade_count for m in markets),
        "winning_market_count": winners,
        # profit_factor = gross_profit / gross_loss; 0 loss → None (avoid inf)
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
    }
