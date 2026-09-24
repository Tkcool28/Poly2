"""DB-backed paper experiment summaries; no synthetic source-side P&L."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.models import Market, PaperOrder, Position, Signal, Trade, Wallet


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower, upper = int(rank), min(len(ordered) - 1, int(rank) + 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower), 3)


async def wallet_paper_evidence(session: AsyncSession, wallet: Wallet) -> dict:
    """Lifetime counts with an explicitly capped distribution sample."""
    source_count = int(await session.scalar(
        select(func.count(Trade.id)).where(Trade.wallet_id == wallet.id)
    ) or 0)
    boundary = wallet.approved_at
    eligible_count = 0
    if boundary is not None:
        eligible_count = int(await session.scalar(
            select(func.count(Trade.id)).where(
                Trade.wallet_id == wallet.id, Trade.traded_at >= boundary,
                Trade.ingested_at >= boundary,
            )
        ) or 0)
    signal_count = int(await session.scalar(
        select(func.count(Signal.id)).where(Signal.wallet_id == wallet.id)
    ) or 0)
    deferrals = int(await session.scalar(
        select(func.coalesce(func.sum(Signal.kill_switch_deferrals), 0))
        .where(Signal.wallet_id == wallet.id)
    ) or 0)
    order_rows = (await session.execute(
        select(PaperOrder.status, PaperOrder.miss_reason, func.count(PaperOrder.id))
        .where(PaperOrder.wallet_id == wallet.id)
        .group_by(PaperOrder.status, PaperOrder.miss_reason)
    )).all()
    copied = sum(count for status, _, count in order_rows if status in ("filled", "partial"))
    partial = sum(count for status, _, count in order_rows if status == "partial")
    misses = sum(count for status, _, count in order_rows if status == "missed")
    reasons = {reason or "unspecified": count for status, reason, count in order_rows
               if status == "missed"}

    sample = (await session.execute(
        select(Signal, PaperOrder)
        .outerjoin(PaperOrder, PaperOrder.signal_id == Signal.id)
        .where(Signal.wallet_id == wallet.id)
        .order_by(Signal.id.desc()).limit(5000)
    )).all()
    lags = [max(0.0, (_aware(s.t1_detected_at) - _aware(s.t0_traded_at)).total_seconds())
            for s, _ in sample]
    adverse_slippage = [
        float((Decimal(str(o.fill_price)) - Decimal(str(s.source_price)))
              * (1 if s.side == "BUY" else -1))
        for s, o in sample if o is not None and o.fill_price is not None
        and o.status in ("filled", "partial")
    ]
    positions = (await session.execute(
        select(Position, Market).join(Market, Market.id == Position.market_id)
        .where(Position.wallet_id == wallet.id)
        .order_by(Position.id).limit(1000)
    )).all()
    open_positions = [
        {"market_id": p.market_id, "market_question": m.question,
         "exposure_usd": round(float(p.quantity * p.avg_price), 6)}
        for p, m in positions if p.quantity > 0 and p.settled_at is None
    ]
    realized_pnl = float(await session.scalar(
        select(func.coalesce(func.sum(Position.realized_pnl), 0))
        .where(Position.wallet_id == wallet.id)
    ) or 0)
    sell_pnl = float(await session.scalar(
        select(func.coalesce(func.sum(PaperOrder.realized_pnl_delta), 0))
        .where(PaperOrder.wallet_id == wallet.id)
    ) or 0)
    settlement_pnl = float(await session.scalar(
        select(func.coalesce(func.sum(Position.settlement_realized_pnl), 0))
        .where(Position.wallet_id == wallet.id)
    ) or 0)
    settled_count = int(await session.scalar(
        select(func.count(Position.id)).where(
            Position.wallet_id == wallet.id, Position.settled_at.is_not(None),
        )
    ) or 0)
    open_exposure = float(await session.scalar(
        select(func.coalesce(func.sum(Position.quantity * Position.avg_price), 0))
        .where(Position.wallet_id == wallet.id, Position.quantity > 0,
               Position.settled_at.is_(None))
    ) or 0)
    prior_settled_without_breakdown = int(await session.scalar(
        select(func.count(Position.id)).where(
            Position.wallet_id == wallet.id, Position.settled_at.is_not(None),
            Position.settlement_realized_pnl.is_(None),
        )
    ) or 0)
    prior_sell_without_breakdown = int(await session.scalar(
        select(func.count(PaperOrder.id)).where(
            PaperOrder.wallet_id == wallet.id, PaperOrder.side == "SELL",
            PaperOrder.status.in_(["filled", "partial"]),
            PaperOrder.realized_pnl_delta.is_(None),
        )
    ) or 0)
    return {
        "wallet_id": wallet.id, "wallet": wallet.address,
        "approval_state": wallet.approval_state,
        "approved_at": boundary.isoformat() if boundary else None,
        "source_trades_observed": source_count,
        "eligible_source_trades": eligible_count,
        "signals_generated": signal_count,
        "copied_trades": copied, "partial_fills": partial,
        "misses": misses, "miss_reasons": reasons,
        "stale_signals": reasons.get("stale_signal", 0),
        "kill_switch_deferrals": deferrals,
        "copy_rate": round(copied / eligible_count, 4) if eligible_count else None,
        "median_detection_lag_seconds": _percentile(lags, 0.5),
        "p90_detection_lag_seconds": _percentile(lags, 0.9),
        "p95_detection_lag_seconds": _percentile(lags, 0.95),
        "median_adverse_slippage": _percentile(adverse_slippage, 0.5),
        "mean_adverse_slippage": (round(sum(adverse_slippage) / len(adverse_slippage), 6)
                                   if adverse_slippage else None),
        "settled_copied_positions": settled_count,
        "sell_realized_pnl_usd": sell_pnl,
        "settlement_realized_pnl_usd": settlement_pnl,
        "pnl_breakdown_incomplete": bool(
            prior_settled_without_breakdown or prior_sell_without_breakdown
        ),
        "copied_realized_pnl_usd": realized_pnl,
        "open_exposure_usd": round(open_exposure, 6),
        "per_market_open_exposure": open_positions,
        "distribution_sample_size": len(sample),
        "distribution_sample_truncated": signal_count > len(sample),
        "position_sample_truncated": len(positions) == 1000,
        "source_wallet_performance_comparison": {
            "available": False,
            "reason": "Source realized P&L for the same observed copy window is not reconciled; no comparable source outcome is fabricated.",
        },
    }
