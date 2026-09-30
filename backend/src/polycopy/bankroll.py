"""Paper bankroll: cash awareness, profit sweeps, stop-loss floor.

The paper bot behaves like a live-money bot with a fixed stake:

* It KNOWS its cash balance (starting bankroll + realized P&L − sweeps).
* New BUYs must fit in available cash (balance minus open-position cost).
* When accumulated un-withdrawn profit reaches ``profit_limit_usd``, the
  whole profit pile is swept OUT (``profit_withdrawal``) — simulating a
  live bot pulling profits to your real account. The bankroll drops back
  to its base and keeps going.
* When the balance falls to ``stop_loss_floor_usd``, new BUYs halt.
  Existing positions still sell and settle — the bot stops digging, it
  does not abandon the shovel mid-hole.

Deliberately absent: any deposit/refill path. A drawdown is a drawdown,
so paper behavior is exactly what live behavior will be.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.config import get_settings
from polycopy.logging_config import get_logger
from polycopy.models import (
    BankrollAccount,
    BankrollLedgerEntry,
    DecisionLogEntry,
    Position,
)

logger = get_logger("polycopy.bankroll")

LEDGER_REALIZED_PNL = "realized_pnl"
LEDGER_PROFIT_WITHDRAWAL = "profit_withdrawal"

ZERO = Decimal(0)

# The account row is a singleton; fixed id keeps get-or-create race-free
# (a concurrent duplicate insert dies on the primary key, and the loser
# re-reads the winner's row).
BANKROLL_ACCOUNT_ID = 1


async def get_bankroll_account(
    session: AsyncSession, *, lock: bool = False
) -> BankrollAccount:
    """Fetch the singleton account row, creating it from settings once.

    ``lock=True`` takes a row-level write lock so the bot cycle and a
    concurrent settings change cannot interleave balance mutations
    (no-op on SQLite, correct on Postgres).
    """
    stmt = select(BankrollAccount).where(BankrollAccount.id == BANKROLL_ACCOUNT_ID)
    if lock:
        stmt = stmt.with_for_update()
    account = (await session.execute(stmt)).scalar_one_or_none()
    if account is None:
        settings = get_settings()
        account = BankrollAccount(
            id=BANKROLL_ACCOUNT_ID,
            starting_bankroll_usd=Decimal(str(settings.starting_bankroll_usd)),
            realized_pnl_total=ZERO,
            withdrawn_total=ZERO,
            profit_limit_usd=Decimal(str(settings.profit_limit_usd)),
            stop_loss_floor_usd=Decimal(str(settings.stop_loss_floor_usd)),
        )
        session.add(account)
        await session.flush()
        logger.info(
            "bankroll_initialized",
            starting=str(account.starting_bankroll_usd),
            profit_limit=str(account.profit_limit_usd),
            stop_loss_floor=str(account.stop_loss_floor_usd),
        )
    return account


def account_balance(account: BankrollAccount) -> Decimal:
    return account.balance


async def open_position_cost(session: AsyncSession) -> Decimal:
    """Cost basis (qty × avg_price) of all open paper positions."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Position.quantity * Position.avg_price), 0)).where(
            Position.quantity > 0,
            Position.settled_at.is_(None),
        )
    )
    return Decimal(str(total))


def stop_loss_hit(account: BankrollAccount) -> bool:
    """Balance at/below the floor → stop opening NEW positions.

    Floor of 0/NULL disables the halt entirely.
    """
    floor = account.stop_loss_floor_usd
    if floor is None:
        return False
    floor = Decimal(str(floor))
    return floor > ZERO and account.balance <= floor


async def post_realized_pnl(
    session: AsyncSession,
    amount: Decimal,
    *,
    context: dict[str, Any],
) -> BankrollLedgerEntry | None:
    """Post a realized-P&L movement (paper SELL or settlement).

    Zero deltas are skipped (no ledger noise for scratch exits). Losses
    are negative entries — they reduce the balance like real money would.
    """
    if amount == ZERO:
        return None
    account = await get_bankroll_account(session, lock=True)
    account.realized_pnl_total = Decimal(str(account.realized_pnl_total)) + amount
    entry = BankrollLedgerEntry(
        entry_type=LEDGER_REALIZED_PNL,
        amount=amount,
        balance_after=account.balance,
        context=context,
    )
    session.add(entry)
    return entry


async def sweep_profit_if_due(session: AsyncSession) -> Decimal:
    """Sweep un-withdrawn profit out once it reaches the profit limit.

    Sweeps the ENTIRE un-withdrawn profit pile (not just the limit
    amount), so the bankroll always returns to its base after a sweep —
    mirroring "cash the profit out, keep trading with the stake".
    Only profit above prior sweeps is ever moved; losses already eaten
    are never re-deposited. Returns the swept amount (0 when not due).
    """
    account = await get_bankroll_account(session, lock=True)
    if account.profit_limit_usd is None:
        return ZERO
    limit = Decimal(str(account.profit_limit_usd))
    if limit <= ZERO:
        return ZERO
    unwithdrawn = max(
        ZERO,
        Decimal(str(account.realized_pnl_total)) - Decimal(str(account.withdrawn_total)),
    )
    if unwithdrawn < limit:
        return ZERO
    account.withdrawn_total = Decimal(str(account.withdrawn_total)) + unwithdrawn
    session.add(
        BankrollLedgerEntry(
            entry_type=LEDGER_PROFIT_WITHDRAWAL,
            amount=-unwithdrawn,
            balance_after=account.balance,
            context={"profit_limit_usd": str(limit)},
        )
    )
    session.add(
        DecisionLogEntry(
            actor="bot",
            action="profit_withdrawn",
            context={
                "amount": str(unwithdrawn),
                "profit_limit_usd": str(limit),
                "balance_after": str(account.balance),
            },
        )
    )
    logger.info("profit_swept", amount=str(unwithdrawn), balance=str(account.balance))
    return unwithdrawn


async def bankroll_view(session: AsyncSession, account: BankrollAccount) -> dict:
    """Dashboard payload: state, limits, and live cash availability."""
    cost = await open_position_cost(session)
    balance = account.balance
    unwithdrawn = max(
        ZERO,
        Decimal(str(account.realized_pnl_total)) - Decimal(str(account.withdrawn_total)),
    )
    return {
        "starting_bankroll_usd": float(account.starting_bankroll_usd),
        "bankroll_balance_usd": float(balance),
        "realized_pnl_total_usd": float(account.realized_pnl_total),
        "withdrawn_total_usd": float(account.withdrawn_total),
        "unwithdrawn_profit_usd": float(unwithdrawn),
        "profit_limit_usd": (
            float(account.profit_limit_usd)
            if account.profit_limit_usd is not None
            else None
        ),
        "stop_loss_floor_usd": (
            float(account.stop_loss_floor_usd)
            if account.stop_loss_floor_usd is not None
            else None
        ),
        "open_cost_usd": float(cost),
        "available_cash_usd": float(balance - cost),
        "stop_loss_hit": stop_loss_hit(account),
    }
