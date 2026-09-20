"""Accounting service: trades + settlements → validated wallet numbers."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.accounting.service import compute_wallet_accounting, reconcile_wallet
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
