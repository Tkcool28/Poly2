"""Deterministic DB/API-service harness for the whole paper lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polycopy.accounting.settlements import refresh_settlements
from polycopy.api.routes import WalletCreateIn, add_wallet, transition_wallet
from polycopy.bot.daemon import score_candidates_if_due
from polycopy.config import get_settings
from polycopy.execution.service import detect_signals, execute_signal, settle_paper_positions
from polycopy.ingestion.catchup import ingest_approved_wallet
from polycopy.main import list_positions, list_signals, paper_evidence
from polycopy.models import (
    ApprovalQueueEntry,
    Base,
    PaperOrder,
    Position,
    Settlement,
    Signal,
    Trade,
    Wallet,
)

ADDRESS = "0x" + "a" * 40


class FakeApis:
    def __init__(self, rows):
        self.rows = rows
        self.trade_calls = []
        self.book_calls = 0
        self.new_market_closed = False
        self.book = {"bids": [{"price": "0.48", "size": "100"}],
                     "asks": [{"price": "0.50", "size": "100"}]}

    async def get_trades(self, wallet, *, limit, offset=0, start=None, end=None):
        assert wallet == ADDRESS
        self.trade_calls.append((limit, offset, end))
        rows = [r for r in self.rows if end is None or r["timestamp"] <= end]
        return rows[offset:offset + limit]

    async def get_gamma_market(self, condition_id, *, closed=None):
        if condition_id == "market-new" and not self.new_market_closed:
            return None if closed else {"conditionId": condition_id, "closed": False}
        if condition_id == "market-open":
            return None if closed else {"conditionId": condition_id, "closed": False}
        return {"conditionId": condition_id, "closed": True,
                "outcomes": ["Yes", "No"], "outcomePrices": ["1", "0"],
                "clobTokenIds": ["123", "456"]}

    async def get_gamma_market_by_token(self, *_args, **_kwargs):
        return None

    async def get_order_book(self, token_id):
        assert token_id == "123"
        self.book_calls += 1
        return self.book

    async def get_clob_market_info(self, condition_id):
        assert condition_id == "market-new"
        return {"fd": {"r": 0, "e": 1, "to": True}}


def trade(index: int, timestamp: datetime, *, market: str) -> dict:
    return {"transactionHash": f"0x{index:064x}", "proxyWallet": ADDRESS,
            "asset": "123", "conditionId": market, "size": 10,
            "price": 0.5, "timestamp": int(timestamp.timestamp()),
            "side": "BUY", "outcome": "Yes"}


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


@pytest.fixture
def paper_settings(monkeypatch):
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "true")
    monkeypatch.setenv("POLYCOPY_MAX_SIGNAL_EXECUTION_AGE_SECONDS", "300")
    monkeypatch.setenv("POLYCOPY_BOOTSTRAP_PAGE_SIZE", "10")
    monkeypatch.setenv("POLYCOPY_BOOTSTRAP_PAGES_PER_RUN", "25")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.mark.parametrize("book_size,expected_status", [(100, "filled"), (5, "partial")])
async def test_candidate_to_api_paper_pnl_with_restart_boundaries(
    session, paper_settings, book_size, expected_status
):
    now = datetime.now(UTC).replace(microsecond=0)
    historic = []
    for i in range(16):
        decided = now - timedelta(days=20 + i)
        historic.extend([
            trade(2 * i, decided, market=f"market-{i}"),
            trade(2 * i + 1, decided + timedelta(hours=1), market=f"market-{i}"),
        ])
    historic.append(trade(32, now - timedelta(days=1), market="market-open"))
    historic.sort(key=lambda r: r["timestamp"], reverse=True)
    api = FakeApis(historic)

    # Supported manual intake; automatic scoring owns bootstrap + verdict.
    added = await add_wallet(WalletCreateIn(address=ADDRESS), session)
    wallet_id = added["id"]
    assert added["approval_state"] == "discovered"
    next_at, verdicts = await score_candidates_if_due(session, api, 0, clock=100)
    assert verdicts[ADDRESS] == "pending_review"
    assert next_at > 100
    assert len(api.trade_calls) > 1
    assert (await session.get(Wallet, wallet_id)).approval_state == "pending_review"
    assert (await session.scalar(select(func.count(ApprovalQueueEntry.id)))) == 1
    assert await detect_signals(session) == 0

    # Human-only transition creates approved_at; historic rows never signal.
    assert (await transition_wallet(wallet_id, "approve", session))["approval_state"] == "approved"
    wallet = await session.get(Wallet, wallet_id)
    await session.refresh(wallet)
    assert wallet.approved_at is not None
    approved_at = wallet.approved_at
    assert await detect_signals(session) == 0

    await asyncio.sleep(1.1)
    new_trade_at = datetime.now(UTC).replace(microsecond=0)
    api.rows = [trade(33, new_trade_at, market="market-new")] + historic
    await ingest_approved_wallet(session, api, wallet)
    # Restart boundary: trade persisted, detection has not run yet.
    session.expire_all()
    assert await detect_signals(session) == 1
    assert await detect_signals(session) == 0
    signal = (await session.execute(select(Signal))).scalar_one()
    assert signal.t0_traded_at.replace(tzinfo=UTC) >= approved_at.replace(tzinfo=UTC)

    # Restart boundary: pending signal survives with the original t1 clock.
    signal_id = signal.id
    session.expire_all()
    signal = await session.get(Signal, signal_id)
    decision_at = signal.t1_detected_at.replace(tzinfo=UTC) + timedelta(seconds=31)
    api.book["asks"][0]["size"] = str(book_size)
    assert await execute_signal(session, api, signal,
                                now=decision_at - timedelta(seconds=2)) is None
    assert api.book_calls == 0
    # Switch still ON even after review delay: no CLOB request.
    assert await execute_signal(session, api, signal, now=decision_at) is None
    assert api.book_calls == 0
    paper_settings.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")
    get_settings.cache_clear()
    order = await execute_signal(session, api, signal, now=decision_at)
    assert order.status == expected_status
    assert api.book_calls == 1
    assert order.requested_size_usd == 10
    assert order.book_depth_shares == book_size
    assert order.levels_consumed == 1
    assert await session.scalar(select(func.count(PaperOrder.id))) == 1

    # Duplicate source page and restart cannot double-signal or double-order.
    session.expire_all()
    wallet = await session.get(Wallet, wallet_id)
    await ingest_approved_wallet(session, api, wallet)
    assert await detect_signals(session) == 0
    assert await session.scalar(select(func.count(PaperOrder.id))) == 1
    assert await session.scalar(select(func.count(Trade.id)).where(Trade.wallet_id == wallet_id)) == 34

    # Gamma initially has no closed row; never fabricate settlement.
    first = await refresh_settlements(
        session, api, condition_ids=["market-new"], now=decision_at,
    )
    assert first["settled"] == 0
    api.new_market_closed = True
    second = await refresh_settlements(
        session, api, condition_ids=["market-new"], now=decision_at + timedelta(seconds=60),
    )
    assert second["settled"] == 1
    session.expire_all()  # restart between execution and paper settlement
    assert (await settle_paper_positions(session, now=decision_at + timedelta(seconds=61)))["settled"] == 1
    assert (await settle_paper_positions(session, now=decision_at + timedelta(seconds=62)))["settled"] == 0
    position = (await session.execute(select(Position).where(Position.wallet_id == wallet_id))).scalar_one()
    assert position.realized_pnl > 0
    assert position.settled_at is not None
    assert await session.scalar(select(func.count(Settlement.id)).where(Settlement.market_id == position.market_id)) == 1

    # Dashboard/API reads the same persisted evidence, including copy P&L.
    evidence = (await paper_evidence(session))["items"][0]
    assert evidence["source_trades_observed"] == 34
    assert evidence["copied_trades"] == 1
    assert evidence["settled_copied_positions"] == 1
    assert evidence["copied_realized_pnl_usd"] > 0
    assert evidence["source_wallet_performance_comparison"]["available"] is False
    assert (await list_positions(session))["totals"]["realized_pnl_usd"] > 0
    assert (await list_signals(session))["items"][0]["paper_order"]["status"] == expected_status


async def test_approved_source_outage_recovers_over_500_without_duplicate_signals(
    session, paper_settings
):
    paper_settings.setenv("POLYCOPY_CATCH_UP_PAGES_PER_CYCLE", "1")
    get_settings.cache_clear()
    base = datetime.now(UTC) - timedelta(hours=2)
    wallet = Wallet(address=ADDRESS, approval_state="approved", approved_at=base - timedelta(hours=1))
    session.add(wallet)
    await session.commit()
    baseline = trade(0, base - timedelta(days=1), market="market-old")
    api = FakeApis([baseline])
    await ingest_approved_wallet(session, api, wallet)
    api.rows = [trade(i, base + timedelta(seconds=i), market="market-new")
                for i in range(620, 0, -1)] + [baseline]
    await ingest_approved_wallet(session, api, wallet)
    wallet_id = wallet.id
    session.expire_all()  # recover continuation from DB, not an in-memory cursor
    wallet = await session.get(Wallet, wallet_id)
    await ingest_approved_wallet(session, api, wallet)
    assert await session.scalar(select(func.count(Trade.id)).where(Trade.wallet_id == wallet.id)) == 621
    for _ in range(4):
        await detect_signals(session)
    assert await session.scalar(select(func.count(Signal.id))) == 620
    await ingest_approved_wallet(session, api, wallet)
    assert await detect_signals(session) == 0
    assert await session.scalar(select(func.count(PaperOrder.id))) == 0
