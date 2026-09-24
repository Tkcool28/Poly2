"""Bounded, resumable candidate-wallet history bootstrap tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.api.routes import get_wallet_bootstrap
from polycopy.ingestion.client import PolymarketAPIError, PolymarketClient
from polycopy.ingestion.service import bootstrap_wallet_history, ingest_wallet_trades
from polycopy.models import Base, Market, Settlement, Trade, Wallet

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def trades(count: int = 30) -> list[dict]:
    result = []
    for index in range(count):
        market = index // 2
        days_ago = 1 if index < 15 else 40
        result.append({
            "transactionHash": f"0x{index:064x}",
            "proxyWallet": "0x" + "1" * 40,
            "asset": str(1000 + market),
            "conditionId": f"condition-{market}",
            "size": 10,
            "price": 0.5,
            "timestamp": int((NOW - timedelta(days=days_ago)).timestamp()) - index,
            "side": "BUY",
            "outcome": "Yes",
        })
    return result


class PagedClient:
    def __init__(self, rows: list[dict], *, overlap: dict[int, list[dict]] | None = None):
        self.rows = rows
        self.calls: list[dict] = []
        self.overlap = overlap or {}

    async def get_trades(self, wallet, *, limit, offset=0, start=None, end=None):
        self.calls.append({"wallet": wallet, "limit": limit, "offset": offset, "start": start, "end": end})
        if offset in self.overlap:
            return self.overlap[offset]
        return self.rows[offset:offset + limit]

    async def get_gamma_market(self, condition_id, *, closed=None):
        return {
            "conditionId": condition_id, "closed": True,
            "outcomes": ["Yes", "No"], "outcomePrices": ["1", "0"],
        }

    async def get_gamma_market_by_token(self, *_args, **_kwargs):
        return None


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


@pytest.fixture(autouse=True)
def bootstrap_settings(monkeypatch):
    settings = SimpleNamespace(
        bootstrap_page_size=30,
        bootstrap_max_pages=10,
        bootstrap_max_trades=1000,
        bootstrap_max_requests=50,
        bootstrap_max_settlement_markets=20,
        ingestion_batch_size=500,
    )
    monkeypatch.setattr("polycopy.ingestion.service.get_settings", lambda: settings)
    return settings


async def _wallet(db):
    wallet = Wallet(address="0x" + "1" * 40, approval_state="discovered")
    db.add(wallet)
    await db.commit()
    return wallet


def _gamma_response(request: httpx.Request) -> httpx.Response:
    cond = request.url.params.get("condition_ids")
    if cond is None:
        return httpx.Response(404)
    return httpx.Response(200, json=[{
        "conditionId": cond,
        "closed": True,
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["1", "0"],
    }])


async def test_mature_history_pages_and_stops_as_soon_as_gates_are_met(session):
    wallet = await _wallet(session)
    rows = trades()
    data_client = PagedClient(rows)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gamma-api.polymarket.com":
            return _gamma_response(request)
        return httpx.Response(200, json=[])

    async with PolymarketClient(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as gamma_client:
        # Exercise real API pagination parameters using a small deterministic page source.
        class Combined:
            async def get_trades(self, *args, **kwargs):
                return await data_client.get_trades(*args, **kwargs)
            async def get_gamma_market(self, *args, **kwargs):
                return await gamma_client.get_gamma_market(*args, **kwargs)
            async def get_gamma_market_by_token(self, *args, **kwargs):
                return await gamma_client.get_gamma_market_by_token(*args, **kwargs)

    result = await bootstrap_wallet_history(session, Combined(), wallet, now=NOW)
    api_status = await get_wallet_bootstrap(wallet.id, session)

    assert result["termination_reason"] == "maturity_satisfied"
    assert api_status["status"]["termination_reason"] == "maturity_satisfied"
    assert result["maturity_satisfied"] is True
    assert result["total_local_trade_count"] == 30
    assert result["settled_market_count"] == 15
    assert len(data_client.calls) == 1
    assert data_client.calls[0]["offset"] == 0
    assert data_client.calls[0]["start"] == 1
    assert data_client.calls[0]["end"] == int(NOW.timestamp())


async def test_hard_limit_is_visible_and_never_loops(session, bootstrap_settings):
    wallet = await _wallet(session)
    bootstrap_settings.bootstrap_page_size = 10
    bootstrap_settings.bootstrap_max_pages = 1
    source = PagedClient(trades())
    result = await bootstrap_wallet_history(session, source, wallet, now=NOW)
    assert result["termination_reason"] == "configured_limit_reached"
    assert result["maturity_satisfied"] is False
    assert len(source.calls) == 1
    assert wallet.approval_state == "discovered"


async def test_history_exhaustion_is_distinguished_from_configured_limit(session):
    wallet = await _wallet(session)
    source = PagedClient([])
    result = await bootstrap_wallet_history(session, source, wallet, now=NOW)
    assert result["termination_reason"] == "history_exhausted"
    assert result["total_local_trade_count"] == 0
    assert len(source.calls) == 1


async def test_interrupted_run_resumes_from_saved_offset(session, bootstrap_settings):
    wallet = await _wallet(session)
    all_rows = trades()
    bootstrap_settings.bootstrap_page_size = 15
    class InterruptAfterFirstPage(PagedClient):
        async def get_trades(self, wallet, *, limit, offset=0, start=None, end=None):
            if offset == 15:
                raise PolymarketAPIError("temporary outage")
            return await super().get_trades(wallet, limit=limit, offset=offset, start=start, end=end)

    first_source = InterruptAfterFirstPage(all_rows)
    first = await bootstrap_wallet_history(session, first_source, wallet, now=NOW)
    assert first["termination_reason"] == "upstream_error"
    assert first["next_offset"] == 15

    second_source = PagedClient(all_rows)
    second = await bootstrap_wallet_history(session, second_source, wallet, now=NOW)
    assert second["termination_reason"] == "maturity_satisfied"
    assert second_source.calls[0]["offset"] == 15
    assert await session.scalar(select(func.count(Trade.id))) == 30
    assert await session.scalar(select(func.count(Market.id))) == 15
    assert await session.scalar(select(func.count(Settlement.id))) == 15

    # Repeating a completed bootstrap is a no-op and cannot inflate accounting.
    third = await bootstrap_wallet_history(session, PagedClient(all_rows), wallet, now=NOW)
    assert third["maturity_satisfied"] is True
    assert await session.scalar(select(func.count(Trade.id))) == 30


async def test_partial_run_resumes_from_saved_offset_and_deduplicates_overlap(
    session, bootstrap_settings
):
    wallet = await _wallet(session)
    all_rows = trades()
    bootstrap_settings.bootstrap_page_size = 15
    bootstrap_settings.bootstrap_max_pages = 1
    first_source = PagedClient(all_rows)
    first = await bootstrap_wallet_history(session, first_source, wallet, now=NOW)
    assert first["termination_reason"] == "configured_limit_reached"
    assert first["next_offset"] == 15

    bootstrap_settings.bootstrap_max_pages = 2
    second_source = PagedClient(all_rows)
    second = await bootstrap_wallet_history(session, second_source, wallet, now=NOW)
    assert second["termination_reason"] == "maturity_satisfied"
    assert second_source.calls[0]["offset"] == 15
    assert await session.scalar(select(func.count(Trade.id))) == 30
    assert await session.scalar(select(func.count(Market.id))) == 15
    assert await session.scalar(select(func.count(Settlement.id))) == 15

    # Repeating a completed bootstrap is a no-op and cannot inflate accounting.
    third = await bootstrap_wallet_history(session, PagedClient(all_rows), wallet, now=NOW)
    assert third["maturity_satisfied"] is True
    assert await session.scalar(select(func.count(Trade.id))) == 30


async def test_overlapping_api_page_rows_are_canonically_deduplicated(session, bootstrap_settings):
    wallet = await _wallet(session)
    rows = trades(20)
    bootstrap_settings.bootstrap_page_size = 10
    bootstrap_settings.bootstrap_max_pages = 2
    source = PagedClient(rows, overlap={10: rows[5:15]})
    result = await bootstrap_wallet_history(session, source, wallet, now=NOW)
    assert result["termination_reason"] == "configured_limit_reached"
    assert result["total_local_trade_count"] == 15
    assert result["duplicates_skipped"] == 5
    assert await session.scalar(select(func.count(Trade.id))) == 15


async def test_regular_live_tail_remains_one_bounded_request(session):
    wallet = await _wallet(session)
    source = PagedClient(trades())
    # The ordinary ingestion call requests only its existing bounded latest slice.
    result = await ingest_wallet_trades(session, source, wallet, fetch_market_metadata=False)
    assert result.inserted == 30
    assert len(source.calls) == 1
    assert source.calls[0]["limit"] == 500
    assert source.calls[0]["offset"] == 0
