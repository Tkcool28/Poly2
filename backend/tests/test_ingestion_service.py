"""Ingestion service: dedup, bounding, market creation — against real SQL."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.ingestion.client import PolymarketClient
from polycopy.ingestion.service import ingest_wallet_trades, run_ingestion_cycle
from polycopy.models import Base, Market, Trade, Wallet


def _trade(tx="0xtx1", wallet="0xWALLET1", ts=1789795243, size=5, price=0.5,
           asset="4667", cond="0xcond1", side="BUY", outcome="Up"):
    return {
        "transactionHash": tx,
        "proxyWallet": wallet,
        "asset": asset,
        "conditionId": cond,
        "size": size,
        "price": price,
        "timestamp": ts,
        "side": side,
        "outcome": outcome,
    }


GAMMA_MARKET = {
    "conditionId": "0xcond1",
    "question": "Will it rain?",
    "slug": "will-it-rain",
    "clobTokenIds": '["4667", "8761"]',
    "outcomes": '["Up", "Down"]',
    "active": True,
    "closed": False,
}


def _make_client(trades_by_wallet: dict[str, list[dict]]) -> PolymarketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data-api.polymarket.com":
            user = request.url.params.get("user", "")
            assert request.url.params["limit"] == "500"  # bounded batch
            return httpx.Response(200, json=trades_by_wallet.get(user, []))
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=[GAMMA_MARKET])
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    return PolymarketClient(http_client=httpx.AsyncClient(transport=transport))


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


async def _add_wallet(session, address="0xwallet1", state="approved") -> Wallet:
    w = Wallet(address=address, approval_state=state)
    session.add(w)
    await session.commit()
    return w


async def test_ingests_new_trades_and_creates_market(session):
    wallet = await _add_wallet(session)
    trades = [_trade(tx="0xtx1"), _trade(tx="0xtx2", ts=1789795244)]
    async with _make_client({"0xwallet1": trades}) as client:
        inserted = await ingest_wallet_trades(session, client, wallet)
    assert inserted.inserted == 2

    db_trades = (await session.execute(select(Trade))).scalars().all()
    assert len(db_trades) == 2
    assert {t.polymarket_trade_id for t in db_trades} == {
        "data-api:0xtx1:0xwallet1:4667:5:0.5:1789795243",
        "data-api:0xtx2:0xwallet1:4667:5:0.5:1789795244",
    }
    assert all(t.asset_id == "4667" for t in db_trades)

    market = (await session.execute(select(Market))).scalar_one()
    assert market.condition_id == "0xcond1"
    assert market.question == "Will it rain?"
    assert market.clob_token_ids == {"Up": "4667", "Down": "8761"}


async def test_second_run_is_a_noop(session):
    """Dedup via canonical key: re-ingesting identical data inserts nothing."""
    wallet = await _add_wallet(session)
    trades = [_trade()]
    async with _make_client({"0xwallet1": trades}) as client:
        first = await ingest_wallet_trades(session, client, wallet)
        second = await ingest_wallet_trades(session, client, wallet)
        assert first.inserted == 1
        assert second.inserted == 0
    count = await session.scalar(select(func.count(Trade.id)))
    assert count == 1


async def test_same_second_different_tx_are_both_kept(session):
    """The real collision case from the audit: same size/price/second,
    different tx hashes — must NOT collapse."""
    wallet = await _add_wallet(session)
    trades = [_trade(tx="0xa"), _trade(tx="0xb"), _trade(tx="0xc")]
    async with _make_client({"0xwallet1": trades}) as client:
        result = await ingest_wallet_trades(session, client, wallet)
        assert result.inserted == 3


async def test_trade_missing_identity_fields_is_skipped(session):
    wallet = await _add_wallet(session)
    bad = _trade()
    del bad["transactionHash"]
    async with _make_client({"0xwallet1": [bad, _trade()]}) as client:
        result = await ingest_wallet_trades(session, client, wallet)
        assert result.inserted == 1
        assert result.quarantined == 1


async def test_gamma_failure_still_ingests_trades(session):
    """Market metadata is best-effort; a Gamma outage must not block trades."""
    wallet = await _add_wallet(session)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=[_trade()])

    transport = httpx.MockTransport(handler)
    async with PolymarketClient(
        http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        inserted = await ingest_wallet_trades(session, client, wallet)
    assert inserted.inserted == 1
    market = (await session.execute(select(Market))).scalar_one()
    assert market.clob_token_ids is None


async def test_run_ingestion_cycle_sequential_and_isolated(session):
    """One failing wallet must not take down the cycle for others."""
    w1 = await _add_wallet(session, "0xwallet1")
    w2 = await _add_wallet(session, "0xwallet2")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=[GAMMA_MARKET])
        user = request.url.params.get("user", "")
        if user == "0xwallet2":
            return httpx.Response(400, text="boom")
        return httpx.Response(200, json=[_trade(wallet="0xWALLET1")])

    transport = httpx.MockTransport(handler)
    async with PolymarketClient(
        http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        results = await run_ingestion_cycle(session, client)
    assert results.wallets == {"0xwallet1": 1, "0xwallet2": 0}
    assert results.failed_wallets == 1
    assert (await session.scalar(select(func.count(Trade.id)))) == 1
    assert w1.id is not None and w2.id is not None


async def test_cycle_can_restrict_to_approved_wallets(session):
    await _add_wallet(session, "0xwallet1", state="approved")
    await _add_wallet(session, "0xwallet2", state="discovered")
    async with _make_client({"0xwallet1": [_trade()]}) as client:
        results = await run_ingestion_cycle(session, client, include_unapproved=False)
    assert list(results.wallets) == ["0xwallet1"]


async def test_long_canonical_trade_id_persists_and_dedups(session):
    wallet_address = "0x" + "a" * 40
    wallet = await _add_wallet(session, wallet_address)
    raw = _trade(
        tx="0x" + "b" * 64,
        wallet=wallet_address,
        asset="9" * 77,
        size="123456789.123456",
        price="0.987654",
    )
    async with _make_client({wallet_address: [raw]}) as client:
        first = await ingest_wallet_trades(session, client, wallet)
        second = await ingest_wallet_trades(session, client, wallet)

    row = (await session.execute(select(Trade))).scalar_one()
    assert len(row.polymarket_trade_id) > 160
    assert first.inserted == 1
    assert second.inserted == 0
    assert await session.scalar(select(func.count(Trade.id))) == 1


async def test_bad_row_savepoint_does_not_poison_later_row(session, monkeypatch):
    import polycopy.ingestion.service as service_module

    wallet = await _add_wallet(session)
    bad = _trade(tx="0xbad", cond="0xbadcond")
    good = _trade(tx="0xgood", cond="0xgoodcond", ts=1789795244)

    real_get = service_module._get_or_create_market

    async def fail_one(session_, client_, condition_id, *, fetch_metadata):
        if condition_id == "0xbadcond":
            raise RuntimeError("deterministic bad row")
        return await real_get(
            session_,
            client_,
            condition_id,
            fetch_metadata=fetch_metadata,
        )

    monkeypatch.setattr(service_module, "_get_or_create_market", fail_one)
    async with _make_client({"0xwallet1": [bad, good]}) as client:
        result = await ingest_wallet_trades(session, client, wallet)

    assert result.inserted == 1
    assert result.quarantined == 1
    rows = (await session.execute(select(Trade))).scalars().all()
    assert [row.polymarket_trade_id for row in rows] == [
        "data-api:0xgood:0xwallet1:4667:5:0.5:1789795244"
    ]
