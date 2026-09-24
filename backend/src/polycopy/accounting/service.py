"""DB-backed wallet accounting: trades + settlements → validated numbers.

This is the bridge between the pure math in ``pnl.py`` and the database.
It answers, for one wallet:

* realized P&L across resolved markets (cash-flow method)
* settled/open market counts, trade counts, gross profit/loss
* best-effort reconciliation against the Data API ``/positions``
  ``realizedPnl`` figure (probe-verified field, PR-B)

Reconciliation is a REPORT, not a gate: the Data API figure covers the
wallet's whole history while ours covers only ingested trades, so a
mismatch during backfill is expected and logged, never an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.accounting import pnl
from polycopy.ingestion.client import PolymarketAPIError, PolymarketClient
from polycopy.logging_config import get_logger
from polycopy.models import Market, Settlement, Trade, Wallet

logger = get_logger("polycopy.accounting.service")


@dataclass
class WalletAccounting:
    wallet_address: str
    summary: dict[str, Decimal | int]
    markets: list[pnl.MarketAccounting]
    # Reconciliation vs Data API /positions realizedPnl (None if not run)
    api_realized_pnl: Decimal | None = None
    reconciliation_delta: Decimal | None = None


def _assemble_wallet_accounting(
    wallet: Wallet,
    rows: list[tuple[Trade, Market, Settlement | None]],
) -> WalletAccounting:
    """Apply the canonical per-market accounting to already-selected rows."""
    legs_by_market: dict[str, list[pnl.TradeLeg]] = {}
    winners: dict[str, str | None] = {}
    for trade, market, settlement in rows:
        legs_by_market.setdefault(market.condition_id, []).append(
            pnl.TradeLeg(
                market_key=market.condition_id,
                outcome=trade.outcome,
                side=trade.side,
                size=Decimal(str(trade.size)),
                price=Decimal(str(trade.price)),
                fee=Decimal(str(trade.fee)) if trade.fee is not None else Decimal(0),
            )
        )
        winners[market.condition_id] = (
            settlement.winning_outcome if settlement else None
        )

    markets = [
        pnl.account_market(
            condition_id, legs, winning_outcome=winners[condition_id]
        )
        for condition_id, legs in legs_by_market.items()
    ]
    return WalletAccounting(
        wallet_address=wallet.address,
        summary=pnl.summarize(markets),
        markets=markets,
    )

async def compute_wallet_accounting(
    session: AsyncSession,
    wallet: Wallet,
) -> WalletAccounting:
    """Assemble one wallet's accounting from ingested trades + settlements."""
    rows = (
        await session.execute(
            select(Trade, Market, Settlement)
            .join(Market, Trade.market_id == Market.id)
            .outerjoin(
                Settlement,
                (Settlement.market_id == Market.id),
            )
            .where(Trade.wallet_id == wallet.id)
        )
    ).all()

    return _assemble_wallet_accounting(wallet, rows)


async def compute_wallet_accounting_for_decision_window(
    session: AsyncSession,
    wallet: Wallet,
    *,
    now: datetime,
    days: int = 90,
) -> WalletAccounting:
    """Account markets whose first wallet trade is inside the lookback window.

    Decision time is the wallet's first trade timestamp in a market. A market
    is included when cutoff <= first_trade_at <= now. Once included, every
    stored trade leg for that wallet/market is accounted, even if later legs
    fall outside the window. Unresolved markets contribute no realized P&L.
    """
    cutoff = now - timedelta(days=days)
    market_ids = (
        await session.execute(
            select(Trade.market_id)
            .where(Trade.wallet_id == wallet.id)
            .group_by(Trade.market_id)
            .having(
                func.min(Trade.traded_at) >= cutoff,
                func.min(Trade.traded_at) <= now,
            )
        )
    ).scalars().all()

    if not market_ids:
        return _assemble_wallet_accounting(wallet, [])

    rows = (
        await session.execute(
            select(Trade, Market, Settlement)
            .join(Market, Trade.market_id == Market.id)
            .outerjoin(Settlement, Settlement.market_id == Market.id)
            .where(
                Trade.wallet_id == wallet.id,
                Trade.market_id.in_(market_ids),
            )
        )
    ).all()
    return _assemble_wallet_accounting(wallet, rows)

async def reconcile_wallet(
    session: AsyncSession,
    client: PolymarketClient,
    wallet: Wallet,
    *,
    accounting: WalletAccounting | None = None,
) -> WalletAccounting:
    """Compare our realized P&L with the Data API's realizedPnl total.

    Best-effort: /positions reflects the wallet's FULL history, ours only
    ingested trades, so deltas during backfill are expected — logged,
    never raised.
    """
    acct = accounting or await compute_wallet_accounting(session, wallet)
    try:
        positions = await client.get_positions(wallet.address)
    except PolymarketAPIError as exc:
        # Reconciliation must never break accounting.
        logger.warning(
            "reconciliation_fetch_failed", wallet=wallet.address, error=str(exc)
        )
        return acct

    api_total = Decimal(0)
    for pos in positions:
        raw = pos.get("realizedPnl")
        if raw is not None:
            api_total += Decimal(str(raw))
    acct.api_realized_pnl = api_total
    ours = Decimal(str(acct.summary["realized_pnl"]))
    acct.reconciliation_delta = ours - api_total
    logger.info(
        "wallet_reconciled",
        wallet=wallet.address,
        ours=str(ours),
        api=str(api_total),
        delta=str(acct.reconciliation_delta),
    )
    return acct
