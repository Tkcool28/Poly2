"""Accounting service: trades + settlements → validated wallet numbers."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.accounting.service import (
    compute_wallet_accounting,
    compute_wallet_accounting_for_decision_window,
    reconcile_wallet,
)
from polycopy.ingestion.client import PolymarketClient
from polycopy.models import Base, Market, Settlement, Trade, Wallet


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    async def no_sleep(*_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed(session):
    """One wallet, two markets: m1 resolved (Up won), m2 still open."""
    wallet = Wallet(address="0xwallet1", approval_state="approved")
    m1 = Market(condition_id="0xm1", question="?", closed=True, resolved_outcome="Up")
    m2 = Market(condition_id="0xm2", question="?")
    session.add_all([wallet, m1, m2])
    await session.flush()
    session.add(Settlement(market_id=m1.id, winning_outcome="Up"))

    def trade(market, tx, outcome, side, size, price):
        return Trade(
            polymarket_trade_id=f"data-api:{tx}:0xwallet1:1:{size}:{price}:1",
            market_id=market.id,
            wallet_id=wallet.id,
            asset_id="1",
            side=side,
            outcome=outcome,
            size=size,
            price=price,
            traded_at=datetime(2026, 9, 1, tzinfo=UTC),
        )

    session.add_all(
        [
            trade(m1, "0xa", "Up", "BUY", 10, 0.60),  # −6, wins → +10 ⇒ +4
            trade(m1, "0xb", "Down", "BUY", 4, 0.30),  # −1.20
            trade(m2, "0xc", "Up", "BUY", 5, 0.50),  # open market
        ]
    )
    await session.commit()
    return wallet


async def test_compute_wallet_accounting(session):
    wallet = await _seed(session)
    acct = await compute_wallet_accounting(session, wallet)
    s = acct.summary
    # m1: −6 −1.20 +10 = +2.80; m2 open, contributes nothing
    assert s["realized_pnl"] == Decimal("2.80")
    assert s["settled_market_count"] == 1
    assert s["open_market_count"] == 1
    assert s["trade_count"] == 3
    assert s["winning_market_count"] == 1
    assert acct.api_realized_pnl is None  # reconciliation not run yet


def _positions_client(realized_pnls) -> PolymarketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"realizedPnl": str(p)} for p in realized_pnls]
        )

    transport = httpx.MockTransport(handler)
    return PolymarketClient(http_client=httpx.AsyncClient(transport=transport))


async def test_reconcile_matches_when_history_complete(session):
    wallet = await _seed(session)
    client = _positions_client([2.80])
    acct = await reconcile_wallet(session, client, wallet)
    assert acct.api_realized_pnl is not None
    assert acct.api_realized_pnl == Decimal("2.8")
    assert acct.reconciliation_delta == Decimal("0.00")


async def test_reconcile_reports_delta_during_backfill(session):
    """API covers full history; ours only ingested trades → delta logged,
    never raised."""
    wallet = await _seed(session)
    client = _positions_client([100.0, -10.0])  # wallet's full history
    acct = await reconcile_wallet(session, client, wallet)
    assert acct.api_realized_pnl is not None
    assert acct.api_realized_pnl == Decimal("90.0")
    assert acct.reconciliation_delta is not None
    assert acct.reconciliation_delta == Decimal("-87.20")


async def test_reconcile_survives_api_outage(session):
    wallet = await _seed(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    transport = httpx.MockTransport(handler)
    client = PolymarketClient(http_client=httpx.AsyncClient(transport=transport))
    acct = await reconcile_wallet(session, client, wallet)
    # Accounting still computed; reconciliation fields stay None.
    assert acct.summary["realized_pnl"] == Decimal("2.80")
    assert acct.api_realized_pnl is None



async def _add_window_market(
    session,
    wallet: Wallet,
    condition_id: str,
    *,
    first_trade_at: datetime,
    winning_outcome: str | None = "Up",
    legs: list[tuple[str, str, Decimal, Decimal, datetime]] | None = None,
):
    market = Market(
        condition_id=condition_id,
        question="?",
        closed=winning_outcome is not None,
        resolved_outcome=winning_outcome,
    )
    session.add(market)
    await session.flush()
    if winning_outcome is not None:
        session.add(Settlement(market_id=market.id, winning_outcome=winning_outcome))
    trade_legs = legs or [
        ("BUY", "Up", Decimal(10), Decimal("0.50"), first_trade_at)
    ]
    for i, (side, outcome, size, price, traded_at) in enumerate(trade_legs):
        session.add(
            Trade(
                polymarket_trade_id=f"window:{condition_id}:{i}",
                market_id=market.id,
                wallet_id=wallet.id,
                asset_id="1",
                side=side,
                outcome=outcome,
                size=size,
                price=price,
                fee=0,
                traded_at=traded_at,
            )
        )
    await session.flush()
    return market


async def test_decision_window_excludes_old_market_even_with_recent_leg(session):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    cutoff = now - timedelta(days=90)
    wallet = Wallet(address="0xwindowold", approval_state="discovered")
    session.add(wallet)
    await session.flush()

    await _add_window_market(
        session,
        wallet,
        "old-crossing",
        first_trade_at=cutoff - timedelta(seconds=1),
        legs=[
            ("BUY", "Up", Decimal(10), Decimal("0.50"), cutoff - timedelta(seconds=1)),
            ("BUY", "Up", Decimal(10), Decimal("0.50"), cutoff + timedelta(days=10)),
        ],
    )
    await session.commit()

    lifetime = await compute_wallet_accounting(session, wallet)
    recent = await compute_wallet_accounting_for_decision_window(
        session, wallet, now=now, days=90
    )

    assert lifetime.summary["settled_market_count"] == 1
    assert lifetime.summary["realized_pnl"] == Decimal("10.00")
    assert recent.summary["settled_market_count"] == 0
    assert recent.summary["trade_count"] == 0
    assert recent.summary["realized_pnl"] == Decimal(0)


async def test_decision_window_cutoff_is_inclusive_and_keeps_complete_market(session):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    cutoff = now - timedelta(days=90)
    wallet = Wallet(address="0xwindowboundary", approval_state="discovered")
    session.add(wallet)
    await session.flush()

    await _add_window_market(
        session,
        wallet,
        "boundary",
        first_trade_at=cutoff,
        legs=[
            ("BUY", "Up", Decimal(10), Decimal("0.50"), cutoff),
            # Complete-market accounting is retained after selection. This
            # deliberately proves there is no per-leg window predicate.
            ("SELL", "Up", Decimal(2), Decimal("0.75"), now + timedelta(seconds=1)),
        ],
    )
    await session.commit()

    recent = await compute_wallet_accounting_for_decision_window(
        session, wallet, now=now, days=90
    )

    assert recent.summary["settled_market_count"] == 1
    assert recent.summary["trade_count"] == 2
    # -5 + 1.50 cash, 8 winning shares pay $8 => +4.50
    assert recent.summary["realized_pnl"] == Decimal("4.50")
    assert recent.summary["gross_profit"] == Decimal("4.50")
    assert recent.summary["gross_loss"] == Decimal(0)


async def test_decision_window_open_markets_do_not_create_realized_pnl(session):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    wallet = Wallet(address="0xwindowopen", approval_state="discovered")
    session.add(wallet)
    await session.flush()
    await _add_window_market(
        session,
        wallet,
        "open",
        first_trade_at=now - timedelta(days=1),
        winning_outcome=None,
    )
    await session.commit()

    recent = await compute_wallet_accounting_for_decision_window(
        session, wallet, now=now, days=90
    )

    assert recent.summary["settled_market_count"] == 0
    assert recent.summary["open_market_count"] == 1
    assert recent.summary["gross_profit"] == Decimal(0)
    assert recent.summary["gross_loss"] == Decimal(0)
    assert recent.summary["realized_pnl"] == Decimal(0)


async def test_decision_window_repeated_reads_are_deterministic(session):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    wallet = Wallet(address="0xwindowrepeat", approval_state="discovered")
    session.add(wallet)
    await session.flush()
    await _add_window_market(
        session,
        wallet,
        "repeat",
        first_trade_at=now - timedelta(days=10),
    )
    await session.commit()

    first = await compute_wallet_accounting_for_decision_window(
        session, wallet, now=now, days=90
    )
    second = await compute_wallet_accounting_for_decision_window(
        session, wallet, now=now, days=90
    )
    assert first.summary == second.summary
