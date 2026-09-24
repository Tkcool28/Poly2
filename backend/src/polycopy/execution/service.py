"""Signal detection + realistic paper execution + paper settlement.

Flow (docs/paper-execution-model.md):

1. ``detect_signals`` — new ingested trades from APPROVED wallets become
   signals, idempotent on the source trade's canonical identity. t1 is
   the trade's ``ingested_at`` (honest detection time), and the signal
   carries the trade's ``asset_id`` (the CLOB token actually traded).
2. ``execute_signal`` — once eligible (t1 + review_delay_seconds), fetch
   the detection-time CLOB book for the traded token, walk it for a fixed
   ``max_order_size_usd`` notional, and record a paper order
   (filled / partial / missed) with full evidence: t0/t1/t2, source
   price, fill price, book snapshot, depth, fee.
3. ``settle_paper_positions`` — positions still open when their market
   resolves realize at $1 (winner) / $0 (loser), exactly once.

Safety invariants (PR #7 hardening):

* Paper-only: execution fails closed unless runtime is valid paper mode.
* Kill switch DEFERS — no order, no book request, signal stays pending.
* Wallet approval is rechecked at execution time (a wallet disabled
  during the review delay cannot execute).
* Malformed book levels are rejected before walking; one bad signal
  never aborts the rest of the cycle; each cycle is explicitly bounded.

Deliberately NOT here: sizing off the source wallet's bankroll, any live
order path. Paper fills update paper Positions only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
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
    Settlement,
    Signal,
    Trade,
    Wallet,
)

logger = get_logger("polycopy.execution")


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _assert_paper_mode() -> None:
    """Chunk 2 is paper-only. Fail closed on any non-paper runtime.

    This is a safety invariant, not a live path: if the process is
    configured for live trading, this module must refuse to run rather
    than risk paper logic touching real money semantics.
    """
    settings = get_settings()
    if settings.allow_live_trading or not settings.paper_mode:
        raise RuntimeError(
            "execution.service is paper-only: refusing to run with "
            f"allow_live_trading={settings.allow_live_trading}, "
            f"paper_mode={settings.paper_mode}"
        )


def _valid_level(price: Decimal, size: Decimal) -> bool:
    """Economic sanity for one book level (PR #7 hardening).

    Polymarket contracts price in (0, 1]; size must be positive; NaN /
    Infinity are never acceptable. Bad upstream data is dropped, never
    walked — no division-by-zero, no invalid VWAP, no cycle failure.
    """
    if not (price.is_finite() and size.is_finite()):
        return False
    return Decimal(0) < price <= Decimal(1) and size > 0


def _parse_levels(raw_levels: list[dict[str, Any]] | None) -> list[BookLevel]:
    """CLOB book levels: [{"price": "0.52", "size": "123.4"}, ...]."""
    levels = []
    for lv in raw_levels or []:
        try:
            price = Decimal(str(lv["price"]))
            size = Decimal(str(lv["size"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            logger.warning("book_level_unparseable", level=lv, error=str(exc))
            continue
        if not _valid_level(price, size):
            logger.warning("book_level_rejected", level=lv)
            continue
        levels.append(BookLevel(price=price, size=size))
    return levels


async def detect_signals(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Turn new approved-wallet trades into signals. Idempotent.

    A trade gets a signal iff its wallet is currently ``approved``, the
    trade was TRADED and INGESTED at/after the wallet's ``approved_at``
    boundary (pre-approval source activity is history, never copyable), and
    no signal with that source_trade_id exists yet. t1 (detection) is the
    trade's ``ingested_at`` — the honest timestamp of when we saw it, so
    detection-lag evidence stays truthful after restarts/backlogs.

    Bounded per cycle (``signal_detection_batch_size``); NOT EXISTS is
    NULL-safe where NOT IN is not.
    """
    settings = get_settings()
    already_signaled = (
        select(Signal.id)
        .where(Signal.source_trade_id == Trade.polymarket_trade_id)
        .exists()
    )
    rows = (
        await session.execute(
            select(Trade, Wallet)
            .join(Wallet, Trade.wallet_id == Wallet.id)
            .where(
                Wallet.approval_state == "approved",
                Wallet.approved_at.is_not(None),
                Trade.traded_at >= Wallet.approved_at,
                Trade.ingested_at >= Wallet.approved_at,
                ~already_signaled,
            )
            .order_by(Trade.ingested_at)
            .limit(settings.signal_detection_batch_size)
        )
    ).all()

    created = 0
    for trade, wallet in rows:
        session.add(
            Signal(
                wallet_id=wallet.id,
                market_id=trade.market_id,
                source_trade_id=trade.polymarket_trade_id,
                asset_id=trade.asset_id,
                side=trade.side,
                outcome=trade.outcome,
                source_price=trade.price,
                t0_traded_at=trade.traded_at,
                t1_detected_at=trade.ingested_at,
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
        requested_size_usd=get_settings().max_order_size_usd,
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


def _eligible_at(signal: Signal) -> datetime:
    """Earliest execution time: t1 + review delay."""
    settings = get_settings()
    return _aware(signal.t1_detected_at) + timedelta(
        seconds=settings.review_delay_seconds
    )


async def execute_signal(
    session: AsyncSession,
    client: PolymarketClient,
    signal: Signal,
    *,
    now: datetime | None = None,
) -> PaperOrder | None:
    """Simulate filling one signal against the detection-time book.

    Returns the PaperOrder, or None when the signal was DEFERRED (review
    delay not elapsed, or kill switch on) — fresh deferred signals stay
    ``pending`` with no book request. Signals older than the configured
    source-to-decision age become recorded stale misses even with the kill
    switch on; no book or fill is created for them.
    """
    _assert_paper_mode()
    settings = get_settings()
    now = now or datetime.now(UTC)
    size_usd = Decimal(str(settings.max_order_size_usd))
    fee_rate = Decimal(str(settings.paper_fee_rate))

    # Source time is the only honest age boundary for copyability; t1 may
    # already be delayed by source outages or process restarts. An expired
    # signal is a missed paper opportunity, never an executable backlog.
    if (now - _aware(signal.t0_traded_at)).total_seconds() > settings.max_signal_execution_age_seconds:
        order = await _record_skip(session, signal, reason="stale_signal", now=now,
                                   order_kwargs={})
        await session.commit()
        return order

    # --- Deferral gates (no executable order or book request) ------------
    if settings.order_kill_switch:
        signal.kill_switch_deferrals = (signal.kill_switch_deferrals or 0) + 1
        return None
    if now < _eligible_at(signal):
        logger.info("signal_deferred", signal_id=signal.id, reason="review_delay")
        return None

    market = await session.get(Market, signal.market_id)

    # Wallet approval is rechecked AT EXECUTION TIME: a signal detected
    # while approved must not execute if the wallet was disabled/rejected
    # during the review delay (PR #7 hardening).
    wallet = await session.get(Wallet, signal.wallet_id)
    if (
        wallet is None
        or wallet.approval_state != "approved"
        or wallet.approved_at is None
    ):
        order = await _record_skip(session, signal, reason="wallet_not_approved",
                                   now=now, order_kwargs={})
        await session.commit()
        return order

    # --- Fail-closed gates (each miss is recorded, never silent) ----------
    if market is None or market.closed:
        order = await _record_skip(session, signal, reason="market_closed", now=now,
                                   order_kwargs={})
        await session.commit()
        return order

    # The authoritative token identity is the source trade's asset_id —
    # the token the wallet actually traded. Gamma outcome->token metadata
    # is fallback evidence only; it may be missing or stale (PR #7).
    token_id = signal.asset_id or (market.clob_token_ids or {}).get(signal.outcome)
    if not token_id:
        order = await _record_skip(session, signal, reason="no_token_for_outcome",
                                   now=now, order_kwargs={})
        await session.commit()
        return order

    # SELL: never sell more paper shares than we own (no synthetic shorts).
    # Zero position → missed. Smaller position → cap the walk at owned qty
    # so filled_size and the position change always agree (PR #7 review).
    owned: Decimal | None = None
    if signal.side == "SELL":
        position = (
            await session.execute(
                select(Position).where(
                    Position.wallet_id == signal.wallet_id,
                    Position.market_id == signal.market_id,
                    Position.outcome == signal.outcome,
                )
            )
        ).scalar_one_or_none()
        owned = Decimal(str(position.quantity)) if position is not None else Decimal(0)
        if owned <= 0:
            order = await _record_skip(session, signal, reason="no_position_to_sell",
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

    result, fee = walk_book(
        signal.side, size_usd, bids, asks, fee_rate=fee_rate, max_shares=owned
    )
    if result.status == "missed":
        order = await _record_skip(session, signal, reason="no_book_depth", now=now,
                                   order_kwargs={"book_snapshot": snapshot,
                                                 "book_depth_shares": result.depth_available,
                                                 "levels_consumed": result.levels_consumed})
        await session.commit()
        return order

    # Price-zone gate on OUR detection-time execution price (VWAP), never
    # the source wallet's price (PR #7 review): a source trade at 0.82 that
    # is now 0.95 must be missed; a source trade at 0.92 whose execution
    # price is below the cap must fill. Entries only — a SELL at a high
    # price is good, not gated.
    if signal.side == "BUY" and result.fill_price >= Decimal(str(settings.max_copy_price)):
        order = await _record_skip(
            session, signal, reason="price_zone", now=now,
            order_kwargs={"book_snapshot": snapshot,
                          "book_depth_shares": result.depth_available,
                          "levels_consumed": result.levels_consumed},
        )
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
        requested_size_usd=size_usd,
        book_depth_shares=result.depth_available,
        levels_consumed=result.levels_consumed,
        fee=fee,
        book_snapshot=snapshot,
    )
    session.add(order)
    signal.status = "executed"

    # Paper position update (BUY adds, SELL reduces), fee-consistent:
    # BUY fee goes INTO cost basis (avg_price), SELL fee comes OUT of
    # realized P&L — PaperOrder.fee and portfolio P&L can never disagree.
    position = (
        await session.execute(
            select(Position).where(
                Position.wallet_id == signal.wallet_id,
                Position.market_id == signal.market_id,
                Position.outcome == signal.outcome,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        position = Position(
            wallet_id=signal.wallet_id,
            market_id=signal.market_id, outcome=signal.outcome,
            quantity=0, avg_price=0, realized_pnl=0, unrealized_pnl=0,
        )
        session.add(position)
    qty = Decimal(str(position.quantity))
    avg = Decimal(str(position.avg_price))
    if signal.side == "BUY":
        new_qty = qty + result.filled_size
        position.avg_price = (
            (qty * avg + result.filled_size * result.fill_price + fee) / new_qty
            if new_qty > 0 else Decimal(0)
        )
        position.quantity = new_qty
    else:  # SELL
        sell_qty = min(qty, result.filled_size)
        order.realized_pnl_delta = sell_qty * (result.fill_price - avg) - fee
        position.realized_pnl = Decimal(str(position.realized_pnl)) + order.realized_pnl_delta
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
                "depth_available": str(result.depth_available),
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


async def settle_paper_positions(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """Realize paper positions whose market has resolved.

    Paper P&L cannot depend on the source wallet sending a SELL: when the
    market settles, winning shares realize at $1 and losing shares at $0
    against the fee-inclusive cost basis (avg_price). Each position is
    settled exactly once — ``settled_at`` is the idempotency marker, so
    repeated settlement cycles are no-ops.
    """
    now = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(Position, Settlement)
            .join(Settlement, Settlement.market_id == Position.market_id)
            .where(Position.quantity > 0, Position.settled_at.is_(None))
        )
    ).all()

    stats = {"settled": 0, "winners": 0, "losers": 0}
    for position, settlement in rows:
        qty = Decimal(str(position.quantity))
        avg = Decimal(str(position.avg_price))
        won = position.outcome == settlement.winning_outcome
        settle_price = Decimal(1) if won else Decimal(0)
        position.settlement_realized_pnl = qty * (settle_price - avg)
        position.realized_pnl = Decimal(str(position.realized_pnl)) + position.settlement_realized_pnl
        position.quantity = Decimal(0)
        position.settled_at = now
        stats["settled"] += 1
        stats["winners" if won else "losers"] += 1
        session.add(
            DecisionLogEntry(
                actor="bot",
                action="paper_position_settled",
                context={
                    "position_id": position.id,
                    "wallet_id": position.wallet_id,
                    "market_id": position.market_id,
                    "outcome": position.outcome,
                    "winning_outcome": settlement.winning_outcome,
                    "quantity": str(qty),
                    "settle_price": str(settle_price),
                    "avg_price": str(avg),
                },
            )
        )
    if stats["settled"]:
        await session.commit()
        logger.info("paper_positions_settled", **stats)
    return stats


async def run_execution_cycle(
    session: AsyncSession,
    client: PolymarketClient,
) -> dict[str, int]:
    """Detect signals, settle resolved positions, execute eligible signals.

    Bounded: detection is capped by ``signal_detection_batch_size`` and
    each cycle executes at most ``execution_batch_size`` signals (one CLOB
    book request each), oldest first — a backlogged DB can never become
    an unbounded execution storm.

    Failure isolation: one bad book request / malformed signal / unexpected
    exception rolls back THAT signal's transaction, is logged, and the
    cycle continues with the next signal. Idempotency keys prevent
    duplicate orders when the signal is retried on a later cycle.
    """
    _assert_paper_mode()
    settings = get_settings()
    cycle_started_at = datetime.now(UTC)

    created = await detect_signals(session)
    settled = await settle_paper_positions(session, now=cycle_started_at)

    # IDs only: a per-signal rollback expires the identity map, so each
    # signal is re-fetched fresh inside the loop.
    pending_ids = (
        await session.execute(
            select(Signal.id)
            .where(Signal.status == "pending")
            .order_by(Signal.t1_detected_at)
            .limit(settings.execution_batch_size)
        )
    ).scalars().all()

    stats: dict[str, int] = {
        "signals_created": created,
        "filled": 0,
        "partial": 0,
        "missed": 0,
        "deferred": 0,
        "errors": 0,
        "positions_settled": settled["settled"],
    }
    for signal_id in pending_ids:
        signal = await session.get(Signal, signal_id)
        if signal is None or signal.status != "pending":
            continue  # settled by a concurrent path — never double-execute
        # Signals are oldest-first; once one is inside the review delay,
        # all later (newer) ones are too — stop early.
        signal_now = datetime.now(UTC)
        if signal_now < _eligible_at(signal) and not settings.order_kill_switch:
            break
        source_trade_id = signal.source_trade_id
        try:
            # Fresh timestamp per signal: t2 reflects this signal's actual
            # decision/book-snapshot attempt, not the start of the batch.
            order = await execute_signal(session, client, signal, now=signal_now)
        except Exception as exc:
            await session.rollback()
            stats["errors"] += 1
            logger.exception(
                "signal_execution_failed",
                signal_id=signal_id,
                source_trade_id=source_trade_id,
                error=str(exc),
            )
            continue
        if order is None:
            stats["deferred"] += 1
        else:
            stats[order.status] = stats.get(order.status, 0) + 1
            if order.miss_reason == "stale_signal":
                stats["stale"] = stats.get("stale", 0) + 1
    if stats["deferred"]:
        await session.commit()  # one durable count write per bounded batch
    return stats
