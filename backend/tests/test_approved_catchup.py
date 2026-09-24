"""Continuity and restart contracts for bounded approved-wallet recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polycopy.ingestion.catchup import ingest_approved_wallet
from polycopy.models import Base, DecisionLogEntry, Trade, Wallet


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


def row(index: int) -> dict:
    return {
        "transactionHash": f"0x{index:064x}", "proxyWallet": "0x" + "1" * 40,
        "asset": "123", "conditionId": "condition-1", "size": 1,
        "price": 0.5, "side": "BUY", "outcome": "Yes",
        "timestamp": 1_700_000_000 + index,
    }


class Source:
    def __init__(self, rows, *, overlap=None):
        self.rows = rows
        self.overlap = overlap or {}
        self.calls = []

    async def get_trades(self, _wallet, *, limit, offset=0, start=None, end=None):
        self.calls.append((limit, offset, end))
        rows = [item for item in self.rows if end is None or item["timestamp"] <= end]
        return self.overlap.get(offset, rows[offset:offset + limit])

    async def get_gamma_market(self, _condition_id):
        return None


@pytest.fixture
def catchup_config(monkeypatch):
    config = SimpleNamespace(ingestion_batch_size=500, catch_up_pages_per_cycle=2)
    monkeypatch.setattr("polycopy.ingestion.catchup.get_settings", lambda: config)
    return config


async def _state(session, wallet):
    return (await session.execute(
        select(DecisionLogEntry).where(
            DecisionLogEntry.action == "wallet_catch_up",
            DecisionLogEntry.context["wallet_id"].as_integer() == wallet.id,
        ).order_by(DecisionLogEntry.id.desc()).limit(1)
    )).scalar_one().context


async def test_recent_overlap_needs_one_request(session, catchup_config):
    wallet = Wallet(address="0x" + "1" * 40, approval_state="approved",
                    approved_at=datetime(2023, 1, 1, tzinfo=UTC))
    session.add(wallet)
    await session.commit()
    source = Source([row(0)])
    await ingest_approved_wallet(session, source, wallet)
    source.rows = [row(2), row(1), row(0)]
    result = await ingest_approved_wallet(session, source, wallet)
    assert result.inserted == 2
    assert len(source.calls) == 2
    assert not (await _state(session, wallet))["catch_up_incomplete"]


async def test_outage_recovers_across_cycles_and_restart_without_duplicate_trades(
    session, catchup_config
):
    wallet = Wallet(address="0x" + "1" * 40, approval_state="approved")
    session.add(wallet)
    await session.commit()
    source = Source([row(0)])
    await ingest_approved_wallet(session, source, wallet)
    source.rows = [row(i) for i in range(1200, -1, -1)]
    first = await ingest_approved_wallet(session, source, wallet)
    assert first.inserted == 1000
    assert (await _state(session, wallet))["next_offset"] == 1000
    assert (await _state(session, wallet))["catch_up_incomplete"]

    # A new client and session identity map can replay the persisted cursor.
    wallet_id = wallet.id
    session.expire_all()
    wallet = await session.get(Wallet, wallet_id)
    restarted = Source(source.rows)
    second = await ingest_approved_wallet(session, restarted, wallet)
    assert second.inserted == 200
    assert len(restarted.calls) == 2
    assert restarted.calls[0] == (500, 0, None)
    assert restarted.calls[1][1] == 1000
    assert not (await _state(session, wallet))["catch_up_incomplete"]
    assert await session.scalar(select(func.count(Trade.id))) == 1201
    await ingest_approved_wallet(session, restarted, wallet)
    assert await session.scalar(select(func.count(Trade.id))) == 1201


async def test_disabled_wallet_does_not_continue_recovery(session, catchup_config):
    wallet = Wallet(address="0x" + "1" * 40, approval_state="approved")
    session.add(wallet)
    await session.commit()
    source = Source([row(0)])
    await ingest_approved_wallet(session, source, wallet)
    source.rows = [row(i) for i in range(1200, -1, -1)]
    await ingest_approved_wallet(session, source, wallet)
    wallet.approval_state = "disabled"
    await session.commit()
    # The bot's cycle selector must not call this transport for disabled wallets.
    from polycopy.ingestion.service import run_ingestion_cycle

    before = len(source.calls)
    await run_ingestion_cycle(session, source, include_unapproved=False)
    assert len(source.calls) == before
