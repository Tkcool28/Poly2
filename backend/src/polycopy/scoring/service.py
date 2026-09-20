"""DB-backed scoring: assemble WalletStats, score, persist, route verdicts.

Routing rules (single approval state machine — ``wallets.approval_state``
is the only truth):

* Only wallets in ``discovered`` or ``pending_review`` are (re)scored and
  re-verdicted. Human decisions (``approved`` / ``rejected`` / ``disabled``)
  are never overridden by the scorer.
* Verdict ``pending_review`` → wallet state + one open ApprovalQueueEntry.
* Verdict ``insufficient_history`` → stays ``discovered``; automatically
  rescored on future cycles. Lack of evidence is never terminal.
* Verdict ``score_rejected`` → stays ``discovered`` too: machine verdicts
  live in WalletScore + the decision log. ``approval_state = rejected``
  is reserved for HUMAN decisions only.
* Every scoring run writes a WalletScore row (the full component breakdown
  lives in ``behavioral_tags`` as JSON — the "why is this score 63?" answer)
  and a DecisionLogEntry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.accounting.service import compute_wallet_accounting
from polycopy.logging_config import get_logger
from polycopy.models import (
    ApprovalQueueEntry,
    DecisionLogEntry,
    Market,
    Trade,
    Wallet,
    WalletScore,
)
from polycopy.scoring import score as sc

logger = get_logger("polycopy.scoring.service")


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo; treat naive as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def build_wallet_stats(
    session: AsyncSession, wallet: Wallet, *, now: datetime
) -> sc.WalletStats:
    """Assemble the scoring snapshot from accounting + trade/settlement rows."""
    acct = await compute_wallet_accounting(session, wallet)
    summary = acct.summary

    # DECISION timestamp per settled market = wallet's first trade in it.
    # Resolution determines when a result becomes knowable; trade timing
    # determines when the decision was made. Recency/consistency use the
    # latter (review correction, 2026-09-20).
    settled_condition_ids = [m.market_key for m in acct.markets if m.resolved]
    decision_ts_by_market: dict[str, datetime] = {}
    if settled_condition_ids:
        rows = (
            await session.execute(
                select(Market.condition_id, func.min(Trade.traded_at))
                .join(Trade, Trade.market_id == Market.id)
                .where(
                    Trade.wallet_id == wallet.id,
                    Market.condition_id.in_(settled_condition_ids),
                )
                .group_by(Market.condition_id)
            )
        ).all()
        decision_ts_by_market = {cond: _aware(ts) for cond, ts in rows}

    settled_pnl: list[tuple[datetime, Decimal]] = []
    per_market: list[Decimal] = []
    for m in acct.markets:
        if m.resolved and m.realized_pnl is not None:
            per_market.append(m.realized_pnl)
            ts = decision_ts_by_market.get(m.market_key)
            if ts is not None:
                settled_pnl.append((ts, m.realized_pnl))

    bounds = (
        await session.execute(
            select(
                Trade.traded_at,
            )
            .where(Trade.wallet_id == wallet.id)
            .order_by(Trade.traded_at)
        )
    ).scalars().all()

    return sc.WalletStats(
        realized_pnl=Decimal(str(summary["realized_pnl"])),
        settled_market_count=int(summary["settled_market_count"]),
        trade_count=int(summary["trade_count"]),
        gross_profit=Decimal(str(summary["gross_profit"])),
        gross_loss=Decimal(str(summary["gross_loss"])),
        per_market_pnl=per_market,
        weekly_pnl=sc.bucket_weekly(settled_pnl),
        recent_30d_pnl=sc.recent_pnl(settled_pnl, now=now),
        first_trade_at=_aware(bounds[0]) if bounds else None,
        last_trade_at=_aware(bounds[-1]) if bounds else None,
    )


async def _apply_verdict(
    session: AsyncSession, wallet: Wallet, result: sc.ScoreResult
) -> None:
    """Route the verdict into the approval state machine. Never overrides
    human decisions (approved/rejected-by-human/disabled)."""
    if wallet.approval_state not in ("discovered", "pending_review"):
        return
    if result.verdict == "pending_review":
        wallet.approval_state = "pending_review"
        open_entry = (
            await session.execute(
                select(ApprovalQueueEntry).where(
                    ApprovalQueueEntry.wallet_id == wallet.id,
                    ApprovalQueueEntry.state == "pending",
                )
            )
        ).scalar_one_or_none()
        if open_entry is None:
            session.add(ApprovalQueueEntry(wallet_id=wallet.id, state="pending"))
    # insufficient_history / score_rejected / discovered → leave state
    # as-is: the wallet stays in the automatic rescan pool, and
    # "rejected" in approval_state is reserved for human decisions.


async def score_wallet(
    session: AsyncSession, wallet: Wallet, *, now: datetime | None = None
) -> sc.ScoreResult:
    """Score one wallet, persist WalletScore + decision log, route verdict."""
    now = now or datetime.now(UTC)
    stats = await build_wallet_stats(session, wallet, now=now)
    result = sc.score_wallet(stats, now=now)

    pf = None
    if stats.gross_loss > 0:
        pf = float(stats.gross_profit / stats.gross_loss)
    elif stats.gross_profit > 0:
        pf = float("inf")

    session.add(
        WalletScore(
            wallet_id=wallet.id,
            window_days=90,
            profit_factor=pf if pf is not None and pf != float("inf") else None,
            composite_score=result.composite,
            behavioral_tags=[
                {
                    "version": "v1",
                    "eligible": result.eligible,
                    "gate_failures": result.gate_failures,
                    "components": result.components,
                    "weights": sc.WEIGHTS,
                    "verdict": result.verdict,
                    "hard_reject_reason": result.hard_reject_reason,
                }
            ],
        )
    )
    session.add(
        DecisionLogEntry(
            actor="scorer",
            action="wallet_scored",
            context={
                "wallet": wallet.address,
                "eligible": result.eligible,
                "composite": result.composite,
                "verdict": result.verdict,
                "gate_failures": result.gate_failures,
                "hard_reject_reason": result.hard_reject_reason,
            },
        )
    )
    await _apply_verdict(session, wallet, result)
    await session.commit()
    logger.info(
        "wallet_scored",
        wallet=wallet.address,
        eligible=result.eligible,
        composite=result.composite,
        verdict=result.verdict,
    )
    return result


async def score_all_wallets(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, str]:
    """Score every discovered/pending_review wallet. One failure never
    stops the run. Returns {address: verdict}."""
    now = now or datetime.now(UTC)
    wallets = (
        (
            await session.execute(
                select(Wallet).where(
                    Wallet.approval_state.in_(["discovered", "pending_review"])
                )
            )
        )
        .scalars()
        .all()
    )
    results: dict[str, str] = {}
    for wallet in wallets:
        try:
            result = await score_wallet(session, wallet, now=now)
            results[wallet.address] = result.verdict
        except Exception:
            logger.exception("wallet_scoring_failed", wallet=wallet.address)
            await session.rollback()
            results[wallet.address] = "error"
    return results
