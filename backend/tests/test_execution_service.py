"""Execution service: signal detection idempotency, gates, realistic fills."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")
# NOTE: the kill switch is flipped per-test via monkeypatch in the
# _fresh_settings fixture below — never module-level, or it would leak
# into test_config / test_api (which assert the safe default).

from polycopy.config import get_settings
from polycopy.execution.service import (
    detect_signals,
    execute_signal,
    run_execution_cycle,
)
from polycopy.ingestion.client import PolymarketClient
from polycopy.models import (
    Base,
    DecisionLogEntry,
    Market,
    PaperOrder,
    Position,
    Signal,
    Trade,
    Wallet,
)

NOW = datetime(2026, 9, 20, tzinfo=UTC)
BOOK = {
    "bids": [{"price": "0.48", "size": "500"}],
    "asks": [{"price": "0.50", "size": "40"}, {"price": "0.52", "size": "100"}],
}


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


def _make_client(book: dict | Exception | None = None) -> PolymarketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "clob.polymarket.com"
        assert request.url.path == "/book"
        if isinstance(book, Exception):
            raise book
        return httpx.Response(200, json=book or BOOK)

    transport = httpx.MockTransport(handler)
    return PolymarketClient(http_client=httpx.AsyncClient(transport=transport))


async def _seed_approved_trade(
    session,
    *,
    address="0xsmart",
    state="approved",
    side="BUY",
    outcome="Up",
    price=0.45,
    closed=False,
    token_ids=None,
    approved_at=None,
    ingested_at=None,
) -> Trade:
    wallet = Wallet(
        address=address,
        approval_state=state,
        # Copy-enabled boundary: default "approved an hour before the trade
        # was ingested" so ordinary tests pass the post-approval filter.
        approved_at=approved_at if approved_at is not None
        else (NOW - timedelta(hours=1)) if state == "approved" else None,
    )
    session.add(wallet)
    await session.flush()
    market = Market(
        condition_id=f"0xcond{address}",
        question="?",
        closed=closed,
        clob_token_ids=(
            token_ids if token_ids is not None else {"Up": "4667", "Down": "8761"}
        ),
    )
    session.add(market)
    await session.flush()
    trade = Trade(
        polymarket_trade_id=f"data-api:0xtx{address}:{address}:4667:5:{price}:1",
        market_id=market.id,
        wallet_id=wallet.id,
        asset_id="4667",
        side=side,
        outcome=outcome,
        size=5,
        price=price,
        fee=0,
        traded_at=NOW - timedelta(minutes=3),
        ingested_at=ingested_at or NOW,
    )
    session.add(trade)
    await session.commit()
    return trade


async def test_detect_signals_only_approved_and_idempotent(session):
    await _seed_approved_trade(session, address="0xsmart")
    await _seed_approved_trade(session, address="0xrandom", state="discovered")

    assert await detect_signals(session, now=NOW) == 1
    signal = (await session.execute(select(Signal))).scalar_one()
    assert signal.wallet_id is not None
    assert signal.source_price == Decimal("0.45")
    # SQLite stores naive datetimes; compare without tzinfo.
    assert signal.t0_traded_at == (NOW - timedelta(minutes=3)).replace(tzinfo=None)
    assert signal.t1_detected_at == NOW.replace(tzinfo=None)

    # Second pass: no duplicates (idempotent on source_trade_id).
    assert await detect_signals(session, now=NOW) == 0


async def test_execute_full_fill_records_evidence(session):
    trade = await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    # $10 at 0.50 → 20 shares; top ask has 40 → full fill at top of book.
    assert order.status == "filled"
    assert order.fill_price == Decimal("0.5")
    assert order.filled_size == 20
    assert order.t2_decided_at is not None
    assert order.book_snapshot["asks"][0] == ["0.50", "40"]
    assert order.fee == 0
    await session.refresh(signal)
    assert signal.status == "executed"

    position = (await session.execute(select(Position))).scalar_one()
    assert position.quantity == 20
    assert position.avg_price == Decimal("0.5")

    log = (await session.execute(select(DecisionLogEntry))).scalar_one()
    assert log.action == "paper_order_executed"
    assert Decimal(log.context["source_price"]) == Decimal("0.45")  # evidence
    assert trade.polymarket_trade_id == log.context["source_trade_id"]


async def test_partial_fill_when_book_shallow(session):
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    shallow = {"bids": [], "asks": [{"price": "0.50", "size": "10"}]}
    async with _make_client(shallow) as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "partial"  # only $5 of depth for a $10 order
    assert order.filled_size == 10
    await session.refresh(signal)
    assert signal.status == "executed"


async def test_missed_when_no_depth_is_recorded_not_silent(session):
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client({"bids": [], "asks": []}) as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "missed"
    assert order.miss_reason == "no_book_depth"
    assert order.book_snapshot is not None  # evidence preserved even on miss
    await session.refresh(signal)
    assert signal.status == "skipped"


async def test_kill_switch_blocks_and_records(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "true")
    get_settings.cache_clear()
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "missed"
    assert order.miss_reason == "kill_switch"


async def test_no_signals_from_pre_approval_trades(session):
    """PR #7 REVIEW REGRESSION: history must never backfill into signals.

    Trade ingested BEFORE approval → no signal. Trade ingested AFTER
    approval → exactly one signal. Repeated cycles stay idempotent.
    """
    approved_at = NOW - timedelta(hours=1)
    # Pre-approval history (ingested 2h ago — before the boundary).
    await _seed_approved_trade(
        session, address="0xhist", approved_at=approved_at,
        ingested_at=NOW - timedelta(hours=2),
    )
    assert await detect_signals(session, now=NOW) == 0

    # A NEW trade ingested after approval → one signal.
    wallet = (await session.execute(
        select(Wallet).where(Wallet.address == "0xhist")
    )).scalar_one()
    market = (await session.execute(select(Market))).scalar_one()
    session.add(
        Trade(
            polymarket_trade_id="data-api:0xnew:0xhist:4667:5:0.4:2",
            market_id=market.id,
            wallet_id=wallet.id,
            asset_id="4667",
            side="BUY",
            outcome="Up",
            size=5,
            price=0.40,
            fee=0,
            traded_at=NOW - timedelta(minutes=1),
            ingested_at=NOW,  # after approved_at
        )
    )
    await session.commit()
    assert await detect_signals(session, now=NOW) == 1
    # Idempotent across repeated cycles.
    assert await detect_signals(session, now=NOW) == 0
    assert (await session.execute(select(Signal))).scalar_one().source_trade_id == \
        "data-api:0xnew:0xhist:4667:5:0.4:2"


async def test_price_zone_gate_uses_execution_price_not_source(session):
    """PR #7 REVIEW REGRESSION: source_price is evidence, not the gate.

    Case 1: source 0.82 but detection-time ask is 0.95 → missed price_zone.
    Case 2: source 0.92 but realistically executable at 0.50 → fills.
    """
    await _seed_approved_trade(session, address="0xmoved", price=0.82)
    await _seed_approved_trade(session, address="0xcooled", price=0.92)
    await detect_signals(session, now=NOW)
    signals = {
        s.wallet_id: s
        for s in (await session.execute(select(Signal))).scalars().all()
    }

    moved_book = {"bids": [], "asks": [{"price": "0.95", "size": "100"}]}
    cooled_book = {"bids": [], "asks": [{"price": "0.50", "size": "100"}]}

    s_moved = next(s for s in signals.values() if Decimal(str(s.source_price)) == Decimal("0.82"))
    s_cooled = next(s for s in signals.values() if Decimal(str(s.source_price)) == Decimal("0.92"))

    async with _make_client(moved_book) as client:
        order1 = await execute_signal(session, client, s_moved)
    assert order1.status == "missed"
    assert order1.miss_reason == "price_zone"

    async with _make_client(cooled_book) as client:
        order2 = await execute_signal(session, client, s_cooled)
    assert order2.status == "filled"
    assert order2.fill_price == Decimal("0.5")


async def test_sell_without_position_is_missed_no_shorting(session):
    """PR #7 REVIEW REGRESSION: no synthetic shorts in V1."""
    await _seed_approved_trade(session, side="SELL")
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "missed"
    assert order.miss_reason == "no_position_to_sell"
    assert (await session.execute(select(Position))).scalars().all() == []


async def test_sell_capped_to_owned_position_and_positions_agree(session):
    """Position smaller than target → capped sell; filled_size == position Δ."""
    await _seed_approved_trade(session, side="SELL")
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    # We own only 5 shares; the $10 target at 0.48 wants ~20.8.
    session.add(
        Position(
            market_id=signal.market_id, outcome="Up",
            quantity=5, avg_price=0.40, realized_pnl=0, unrealized_pnl=0,
        )
    )
    await session.commit()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "partial"  # target not met, capped by ownership
    assert order.filled_size == 5  # never more than owned
    position = (await session.execute(select(Position))).scalar_one()
    assert position.quantity == 0  # 5 owned − 5 sold
    assert position.realized_pnl == Decimal(5) * (Decimal("0.48") - Decimal("0.40"))


async def test_sell_normal_position_uses_normal_sizing(session):
    """Ample position → the normal $10 target applies, no cap distortion."""
    await _seed_approved_trade(session, side="SELL")
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    session.add(
        Position(
            market_id=signal.market_id, outcome="Up",
            quantity=100, avg_price=0.40, realized_pnl=0, unrealized_pnl=0,
        )
    )
    await session.commit()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    # $10 at bid 0.48 → 20.833... shares, well under the 100 owned.
    assert order.status == "filled"
    assert order.fill_price == Decimal("0.48")
    position = (await session.execute(select(Position))).scalar_one()
    assert position.quantity == (100 - order.filled_size).quantize(
        Decimal("0.000001")
    )  # agreement (column precision is 6dp)


async def test_decision_log_reports_depth_available_not_consumed(session):
    """PR #7 REVIEW: depth evidence is unambiguous — available, not consumed."""
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client() as client:
        await execute_signal(session, client, signal)

    log = (await session.execute(select(DecisionLogEntry))).scalar_one()
    assert "depth_available" in log.context
    assert "depth_consumed" not in log.context
    # 140 asks total; we consumed only the 20 shares we filled.
    assert Decimal(log.context["depth_available"]) == Decimal(140)


async def test_closed_market_and_missing_token_skip(session):
    await _seed_approved_trade(session, address="0xclosed", closed=True)
    await _seed_approved_trade(session, address="0xnotok", token_ids={"Down": "8761"})
    await detect_signals(session, now=NOW)
    signals = (await session.execute(select(Signal))).scalars().all()

    reasons = []
    async with _make_client() as client:
        for s in signals:
            order = await execute_signal(session, client, s)
            reasons.append(order.miss_reason)
    assert sorted(reasons) == ["market_closed", "no_token_for_outcome"]


async def test_exposure_cap_blocks_oversize(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_MAX_EXPOSURE_PER_MARKET_USD", "15")
    get_settings.cache_clear()
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    # Existing $12 position in the same market + $10 order > $15 cap.
    session.add(
        Position(
            market_id=signal.market_id, outcome="Up", quantity=24, avg_price=0.50
        )
    )
    await session.commit()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "missed"
    assert order.miss_reason == "exposure_cap"


async def test_fee_rate_applies_to_fill_notional(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_PAPER_FEE_RATE", "0.01")
    get_settings.cache_clear()
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.fee == Decimal("0.100000")  # 1% of $10


async def test_run_execution_cycle_end_to_end(session):
    await _seed_approved_trade(session, address="0xa")
    await _seed_approved_trade(session, address="0xb")

    async with _make_client() as client:
        stats = await run_execution_cycle(session, client)

    assert stats == {"signals_created": 2, "filled": 2, "partial": 0, "missed": 0}
    count = await session.scalar(select(func.count(PaperOrder.id)))
    assert count == 2
    # Idempotent: a second cycle creates nothing new.
    async with _make_client() as client:
        stats2 = await run_execution_cycle(session, client)
    assert stats2 == {"signals_created": 0, "filled": 0, "partial": 0, "missed": 0}
