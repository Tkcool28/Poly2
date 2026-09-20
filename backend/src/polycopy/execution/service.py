"""Signal detection + realistic paper execution.

Flow (docs/paper-execution-model.md):

1. ``detect_signals`` — new ingested trades from APPROVED wallets become
   signals, idempotent on the source trade's canonical identity.
2. ``execute_signal`` — fetch the detection-time CLOB book for the traded
   token, walk it for a fixed ``max_order_size_usd`` notional, and record
   a paper order (filled / partial / missed) with full evidence: t0/t1/t2,
   source price, fill price, book snapshot, depth, fee.

Deliberately NOT here: sizing off the source wallet's bankroll, any live
order path. Paper fills update paper Positions only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.config import get_settings
from polycopy.execution.bookwalk import BookLevel, walk_book
from polycopy.ingestion.client import PolymarketClient
from polycopy.logging_config import get_logger
from polycopy.models import (
    DecisionLogEntry,
    Market,
    PaperOrder,
    Position,
    Signal,
    Trade,
    Wallet,
)

logger = get_logger("polycopy.execution")


def _parse_levels(raw_levels: list[dict[str, Any]] | None) -> list[BookLevel]:
    """CLOB book levels: [{"price": "0.52", "size": "123.4"}, ...]."""
    levels = []
    for lv in raw_levels or []:
        try:
            levels.append(
                BookLevel(price=Decimal(str(lv["price"])), size=Decimal(str(lv["size"])))
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("book_level_unparseable", level=lv, error=str(exc))
    return levels


async def detect_signals(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Turn new approved-wallet trades into signals. Idempotent.

    A trade gets a signal iff its wallet is currently ``approved`` and no
    signal with that source_trade_id exists yet. t1 (detection) is now —
    the honest timestamp of when we saw it.
    """
    now = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(Trade, Wallet)
            .join(Wallet, Trade.wallet_id == Wallet.id)
            .where(
                Wallet.approval_state == "approved",
                Trade.polymarket_trade_id.not_in(
                    select(Signal.source_trade_id)
                ),
            )
        )
    ).all()

    created = 0
    for trade, wallet in rows:
        session.add(
            Signal(
                wallet_id=wallet.id,
                market_id=trade.market_id,
                source_trade_id=trade.polymarket_trade_id,
                side=trade.side,
                outcome=trade.outcome,
                source_price=trade.price,
                t0_traded_at=trade.traded_at,
                t1_detected_at=now,
            )
        )
        created += 1
    if created:
        await session.commit()
        logger.info("signals_detected", new=created)
    return created


async def _record_skip(
    session: AsyncSession,
    signal: Signal,
    *,
    reason: str,
    now: datetime,
    order_kwargs: dict[str, Any],
) -> PaperOrder:
    """Record a missed/skipped paper order — misses are data."""
    order = PaperOrder(
        idempotency_key=f"paper:{signal.source_trade_id}",
        signal_id=signal.id,
        market_id=signal.market_id,
        wallet_id=signal.wallet_id,
        side=signal.side,
        size=0,
        price=0,
        status="missed",
        t2_decided_at=now,
        miss_reason=reason,
        **order_kwargs,
    )
    session.add(order)
    signal.status = "skipped"
    session.add(
        DecisionLogEntry(
            actor="bot",
            action="signal_skipped",
            context={
                "signal_id": signal.id,
                "source_trade_id": signal.source_trade_id,
                "reason": reason,
            },
        )
    )
    return order


async def execute_signal(
    session: AsyncSession,
    client: PolymarketClient,
    signal: Signal,
    *,
    now: datetime | None = None,
) -> PaperOrder:
    """Simulate filling one signal against the detection-time book."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    size_usd = Decimal(str(settings.max_order_size_usd))
    fee_rate = Decimal(str(settings.paper_fee_rate))

    market = await session.get(Market, signal.market_id)

    # --- Fail-closed gates (each miss is recorded, never silent) ----------
    if settings.order_kill_switch:
        order = await _record_skip(session, signal, reason="kill_switch", now=now,
                                   order_kwargs={})
        await session.commit()
        return order
    if market is None or market.closed:
        order = await _record_skip(session, signal, reason="market_closed", now=now,
                                   order_kwargs={})
        await session.commit()
        return order
    if signal.source_price is not None and Decimal(str(signal.source_price)) >= Decimal(
        str(settings.max_copy_price)
    ):
        order = await _record_skip(session, signal, reason="price_zone", now=now,
                                   order_kwargs={})
        await session.commit()
        return order

    token_id = (market.clob_token_ids or {}).get(signal.outcome)
    if not token_id:
        order = await _record_skip(session, signal, reason="no_token_for_outcome",
                                   now=now, order_kwargs={})
        await session.commit()
        return order

    # Exposure caps (BUYs only — a SELL reduces exposure). Exposure is
    # priced at cost basis: sum(quantity × avg_price) across positions.
    if signal.side == "BUY":
        positions = (await session.execute(select(Position))).scalars().all()
        global_exp = sum(
            (Decimal(str(p.quantity)) * Decimal(str(p.avg_price)) for p in positions),
            Decimal(0),
        )
        market_exp = sum(
            (Decimal(str(p.quantity)) * Decimal(str(p.avg_price))
             for p in positions if p.market_id == signal.market_id),
            Decimal(0),
        )
        if (
            global_exp + size_usd > Decimal(str(settings.max_exposure_global_usd))
            or market_exp + size_usd > Decimal(str(settings.max_exposure_per_market_usd))
        ):
            order = await _record_skip(session, signal, reason="exposure_cap",
                                       now=now, order_kwargs={})
            await session.commit()
            return order

    # --- Detection-time book snapshot --------------------------------------
    book = await client.get_order_book(token_id)
    bids = _parse_levels(book.get("bids"))
    asks = _parse_levels(book.get("asks"))
    snapshot = {
        "bids": [[str(lv.price), str(lv.size)] for lv in bids],
        "asks": [[str(lv.price), str(lv.size)] for lv in asks],
    }

    result, fee = walk_book(signal.side, size_usd, bids, asks, fee_rate=fee_rate)
    if result.status == "missed":
        order = await _record_skip(session, signal, reason="no_book_depth", now=now,
                                   order_kwargs={"book_snapshot": snapshot})
        await session.commit()
        return order

    order = PaperOrder(
        idempotency_key=f"paper:{signal.source_trade_id}",
        signal_id=signal.id,
        market_id=signal.market_id,
        wallet_id=signal.wallet_id,
        side=signal.side,
        size=result.filled_size,
        price=result.fill_price,
        status=result.status,  # filled | partial
        filled_at=now,
        t2_decided_at=now,
        fill_price=result.fill_price,
        filled_size=result.filled_size,
        fee=fee,
        book_snapshot=snapshot,
    )
    session.add(order)
    signal.status = "executed"

    # Paper position update (BUY adds, SELL reduces).
    position = (
        await session.execute(
            select(Position).where(
                Position.market_id == signal.market_id,
                Position.outcome == signal.outcome,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        position = Position(
            market_id=signal.market_id, outcome=signal.outcome,
            quantity=0, avg_price=0, realized_pnl=0, unrealized_pnl=0,
        )
        session.add(position)
    qty = Decimal(str(position.quantity))
    avg = Decimal(str(position.avg_price))
    if signal.side == "BUY":
        new_qty = qty + result.filled_size
        position.avg_price = (
            (qty * avg + result.filled_size * result.fill_price) / new_qty
            if new_qty > 0 else Decimal(0)
        )
        position.quantity = new_qty
    else:  # SELL
        sell_qty = min(qty, result.filled_size)
        position.realized_pnl = Decimal(str(position.realized_pnl)) + sell_qty * (
            result.fill_price - avg
        )
        position.quantity = qty - sell_qty

    session.add(
        DecisionLogEntry(
            actor="bot",
            action="paper_order_executed",
            context={
                "signal_id": signal.id,
                "source_trade_id": signal.source_trade_id,
                "status": result.status,
                "fill_price": str(result.fill_price),
                "source_price": str(signal.source_price),
                "filled_size": str(result.filled_size),
                "depth_consumed": str(result.depth_consumed),
                "levels_consumed": result.levels_consumed,
                "fee": str(fee),
            },
        )
    )
    await session.commit()
    logger.info(
        "paper_order",
        signal_id=signal.id,
        status=result.status,
        fill_price=str(result.fill_price),
        source_price=str(signal.source_price),
    )
    return order


async def run_execution_cycle(
    session: AsyncSession,
    client: PolymarketClient,
) -> dict[str, int]:
    """Detect signals from approved wallets, then execute pending ones.

    Returns counters for the daemon heartbeat. Bounded: signals are created
    only from bounded ingestion batches, and each pending signal costs one
    book request.
    """
    created = await detect_signals(session)
    pending = (
        await session.execute(
            select(Signal).where(Signal.status == "pending")
        )
    ).scalars().all()

    stats = {"signals_created": created, "filled": 0, "partial": 0, "missed": 0}
    for signal in pending:
        order = await execute_signal(session, client, signal)
        stats[order.status] = stats.get(order.status, 0) + 1
    return stats
