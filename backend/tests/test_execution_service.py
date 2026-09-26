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

import polycopy.execution.service as execution_service

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")
# NOTE: the kill switch is flipped per-test via monkeypatch in the
# _fresh_settings fixture below — never module-level, or it would leak
# into test_config / test_api (which assert the safe default).

from polycopy.config import get_settings
from polycopy.execution.service import (
    detect_signals,
    execute_signal,
    run_execution_cycle,
    settle_paper_positions,
)
from polycopy.ingestion.client import PolymarketClient
from polycopy.models import (
    Base,
    DecisionLogEntry,
    Market,
    PaperOrder,
    Position,
    Settlement,
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
    # Existing execution scenarios use fixed 2026-09-20 times; isolate their
    # book/accounting assertions from the new freshness gate. Freshness tests
    # below explicitly use the production default of five minutes.
    monkeypatch.setenv("POLYCOPY_MAX_SIGNAL_EXECUTION_AGE_SECONDS", "10000000")
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


def _make_client(
    book: dict | Exception | None = None,
    fee_data: dict | Exception | None = None,
) -> PolymarketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "clob.polymarket.com"
        if request.url.path.startswith("/clob-markets/"):
            if isinstance(fee_data, Exception):
                raise fee_data
            return httpx.Response(200, json=fee_data if fee_data is not None
                                  else {"fd": {"r": 0, "e": 1, "to": True}})
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
    traded_at=None,
    asset_id="4667",
    trade_id=None,
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
        polymarket_trade_id=(
            trade_id
            if trade_id is not None
            else f"data-api:0xtx{address}:{address}:4667:5:{price}:1"
        ),
        market_id=market.id,
        wallet_id=wallet.id,
        asset_id=asset_id,
        side=side,
        outcome=outcome,
        size=5,
        price=price,
        fee=0,
        traded_at=traded_at or (NOW - timedelta(minutes=3)),
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


async def test_long_source_trade_id_persists_paper_idempotency_key(session):
    long_trade_id = (
        "data-api:"
        + "0x"
        + "b" * 64
        + ":"
        + "0x"
        + "a" * 40
        + ":"
        + "9" * 77
        + ":123456789.123456:0.987654:1789795243"
    )
    assert len(long_trade_id) > 200

    await _seed_approved_trade(session, trade_id=long_trade_id)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    assert signal.source_trade_id == long_trade_id

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.idempotency_key == f"paper:{long_trade_id}"
    assert len(order.idempotency_key) > 200
    persisted = (await session.execute(select(PaperOrder))).scalar_one()
    assert persisted.idempotency_key == order.idempotency_key


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


async def test_kill_switch_defers_without_order_or_book_request(session, monkeypatch):
    """PR #7 HARDENING: kill switch ON blocks execution WITHOUT consuming
    the signal — no PaperOrder, no CLOB book request, signal stays
    pending; once OFF, the signal executes normally (docs/safety.md)."""
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "true")
    get_settings.cache_clear()
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("book request must not happen under kill switch")

    transport = httpx.MockTransport(boom)
    async with PolymarketClient(
        http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        order = await execute_signal(session, client, signal)

    assert order is None
    assert signal.status == "pending"
    assert await session.scalar(select(func.count(PaperOrder.id))) == 0

    # Switch OFF → the same signal becomes eligible and fills normally.
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")
    get_settings.cache_clear()
    async with _make_client() as client:
        order = await execute_signal(session, client, signal)
    assert order.status == "filled"
    assert signal.status == "executed"


async def test_kill_switch_backlog_expires_as_stale_miss_without_book(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "true")
    monkeypatch.setenv("POLYCOPY_MAX_SIGNAL_EXECUTION_AGE_SECONDS", "300")
    get_settings.cache_clear()
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    book_requests = 0

    def book(request: httpx.Request) -> httpx.Response:
        nonlocal book_requests
        if request.url.path.startswith("/clob-markets/"):
            return httpx.Response(200, json={"fd": {"r": 0, "e": 1, "to": True}})
        book_requests += 1
        return httpx.Response(200, json=BOOK)

    async with PolymarketClient(http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(book)
    )) as client:
        assert await execute_signal(session, client, signal, now=NOW + timedelta(minutes=1)) is None
        await session.commit()
        assert signal.kill_switch_deferrals == 1
        stale = await execute_signal(session, client, signal, now=NOW + timedelta(minutes=6))
        assert stale.status == "missed"
        assert stale.miss_reason == "stale_signal"
        assert stale.book_snapshot is None
        assert book_requests == 0
        assert signal.status == "skipped"

        # Enabling paper execution later cannot release the old backlog.
        monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")
        get_settings.cache_clear()
        await _seed_approved_trade(
            session, address="0xfresh", traded_at=NOW + timedelta(minutes=10),
            ingested_at=NOW + timedelta(minutes=10),
        )
        await detect_signals(session, now=NOW + timedelta(minutes=10))
        fresh = (await session.execute(select(Signal).where(Signal.status == "pending"))).scalar_one()
        filled = await execute_signal(
            session, client, fresh, now=NOW + timedelta(minutes=10, seconds=31),
        )
    assert filled.status == "filled"
    assert book_requests == 1
    assert await session.scalar(select(func.count(PaperOrder.id))) == 2


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


async def test_source_trade_before_approval_is_never_copyable(session):
    """Final audit: late ingestion must not revive a source trade that
    actually happened before the wallet was approved."""
    approved_at = NOW - timedelta(hours=1)
    await _seed_approved_trade(
        session,
        address="0xlate",
        approved_at=approved_at,
        traded_at=NOW - timedelta(hours=2),
        ingested_at=NOW,
    )
    assert await detect_signals(session, now=NOW) == 0
    assert await session.scalar(select(func.count(Signal.id))) == 0


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
            wallet_id=signal.wallet_id,
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
            wallet_id=signal.wallet_id,
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
    # No token identity anywhere: trade asset_id missing AND Gamma mapping
    # has no entry for the traded outcome.
    await _seed_approved_trade(
        session, address="0xnotok", token_ids={"Down": "8761"}, asset_id=None
    )
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
            wallet_id=signal.wallet_id,
            market_id=signal.market_id, outcome="Up", quantity=24, avg_price=0.50
        )
    )
    await session.commit()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "missed"
    assert order.miss_reason == "exposure_cap"


async def test_market_fee_uses_actual_fill_and_persists_evidence(session):
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client(fee_data={"fd": {"r": 0.05, "e": 1, "to": True}}) as client:
        order = await execute_signal(session, client, signal)

    assert order.fee == Decimal("0.25000")  # 20 × .05 × .50 × .50
    assert order.fee_enabled is True
    assert order.fee_rate_coefficient == "0.05"
    assert order.fee_exponent == "1"
    assert order.fee_taker_only is True
    assert order.fee_liquidity_role == "taker"
    assert order.fee_source == "GET /clob-markets/{condition_id}"
    assert order.fee_metadata_retrieved_at is not None
    assert Decimal(order.fee_calculation_shares) == 20
    assert Decimal(order.fee_calculation_price) == Decimal("0.5")
    await session.refresh(order)
    assert order.fee == Decimal("0.250000")


async def test_zero_fee_metadata_ignores_obsolete_global_setting(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_PAPER_FEE_RATE", "0.99")
    get_settings.cache_clear()
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    async with _make_client() as client:
        order = await execute_signal(session, client, signal)
    assert order.fee == 0
    assert order.fee_enabled is False
    assert order.fee_rate_coefficient == "0"


async def test_partial_fee_and_settlement_cost_basis(session):
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    book = {"bids": [], "asks": [{"price": "0.50", "size": "10"}]}
    async with _make_client(book, {"fd": {"r": "0.05", "e": 1, "to": True}}) as client:
        order = await execute_signal(session, client, signal)
    assert order.status == "partial"
    assert order.filled_size == 10
    assert order.fee == Decimal("0.12500")
    position = (await session.execute(select(Position))).scalar_one()
    assert position.avg_price == Decimal("0.512500")
    session.add(Settlement(market_id=signal.market_id, winning_outcome="Up"))
    await session.commit()
    assert (await settle_paper_positions(session, now=NOW))["settled"] == 1
    assert position.realized_pnl == Decimal("4.875000")
    assert (await settle_paper_positions(session, now=NOW))["settled"] == 0


async def test_buy_fee_is_included_in_existing_exposure_cap(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_MAX_EXPOSURE_PER_MARKET_USD", "10.10")
    get_settings.cache_clear()
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    async with _make_client(fee_data={"fd": {"r": "0.05", "e": 1, "to": True}}) as client:
        order = await execute_signal(session, client, signal)
    assert order.status == "missed"
    assert order.miss_reason == "exposure_cap"
    assert await session.scalar(select(func.count(Position.id))) == 0


@pytest.mark.parametrize("fee_data", [{}, {"fd": {"r": "NaN", "e": 1, "to": True}}])
async def test_bad_fee_metadata_leaves_signal_pending_without_fill(session, fee_data):
    await _seed_approved_trade(session)
    async with _make_client(fee_data=fee_data) as client:
        stats = await run_execution_cycle(session, client)
    assert stats["errors"] == 1
    assert stats["filled"] == 0
    assert (await session.execute(select(Signal))).scalar_one().status == "pending"
    assert await session.scalar(select(func.count(PaperOrder.id))) == 0


async def test_fee_info_outage_retries_without_zero_fee_fill(session):
    await _seed_approved_trade(session)
    async with _make_client(fee_data=httpx.ConnectError("fee info down")) as client:
        stats = await run_execution_cycle(session, client)
    assert stats["errors"] == 1
    assert (await session.execute(select(Signal))).scalar_one().status == "pending"
    assert await session.scalar(select(func.count(PaperOrder.id))) == 0


async def test_clob_fee_info_uses_market_specific_endpoint():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"fd": {"r": 0, "e": 1, "to": True}})

    async with PolymarketClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as client:
        assert (await client.get_clob_market_info("0xcondition"))["fd"]["r"] == 0
    assert paths == ["/clob-markets/0xcondition"]


async def test_run_execution_cycle_end_to_end(session):
    await _seed_approved_trade(session, address="0xa")
    await _seed_approved_trade(session, address="0xb")

    async with _make_client() as client:
        stats = await run_execution_cycle(session, client)

    assert stats == {
        "signals_created": 2, "filled": 2, "partial": 0, "missed": 0,
        "deferred": 0, "errors": 0, "positions_settled": 0,
    }
    count = await session.scalar(select(func.count(PaperOrder.id)))
    assert count == 2
    # Idempotent: a second cycle creates nothing new.
    async with _make_client() as client:
        stats2 = await run_execution_cycle(session, client)
    assert stats2 == {
        "signals_created": 0, "filled": 0, "partial": 0, "missed": 0,
        "deferred": 0, "errors": 0, "positions_settled": 0,
    }


# ---------------------------------------------------------------------------
# PR #7 FINAL HARDENING REGRESSIONS
# ---------------------------------------------------------------------------


async def test_review_delay_blocks_until_eligible(session, monkeypatch):
    """HARDENING #1: no execution before t1 + review_delay_seconds."""
    monkeypatch.setenv("POLYCOPY_REVIEW_DELAY_SECONDS", "3600")
    get_settings.cache_clear()
    await _seed_approved_trade(session)  # ingested_at = NOW → t1 = NOW
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client() as client:
        # 30 minutes after detection: inside the 1h delay → deferred.
        order = await execute_signal(
            session, client, signal, now=NOW + timedelta(minutes=30)
        )
        assert order is None
        assert signal.status == "pending"
        assert await session.scalar(select(func.count(PaperOrder.id))) == 0

        # After the delay elapses: eligible, fills normally.
        order = await execute_signal(
            session, client, signal, now=NOW + timedelta(hours=1, seconds=1)
        )
        assert order.status == "filled"
        assert signal.status == "executed"


async def test_asset_id_used_when_gamma_mapping_absent(session):
    """HARDENING #2: the source trade's asset_id is the book-request token,
    even when market.clob_token_ids has no entry for the outcome."""
    await _seed_approved_trade(
        session, token_ids={"Down": "8761"}, asset_id="9999"
    )
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    assert signal.asset_id == "9999"  # carried from the trade

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/clob-markets/"):
            return httpx.Response(200, json={"fd": {"r": 0, "e": 1, "to": True}})
        seen.append(request.url.params["token_id"])
        return httpx.Response(200, json=BOOK)

    transport = httpx.MockTransport(handler)
    async with PolymarketClient(
        http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        order = await execute_signal(session, client, signal)

    assert seen == ["9999"]  # NOT the Gamma mapping (which lacks "Up")
    assert order.status == "filled"


async def test_t1_is_honest_ingestion_time(session):
    """HARDENING #8: t1 = Trade.ingested_at, not the detect_signals() run
    time — detection-lag evidence stays truthful after backlogs."""
    ingested = NOW - timedelta(hours=3)
    await _seed_approved_trade(
        session, approved_at=ingested - timedelta(hours=1), ingested_at=ingested
    )
    # Detection query runs 3h later (restart/backlog).
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    assert signal.t1_detected_at == ingested.replace(tzinfo=None)  # SQLite-naive
    assert signal.t1_detected_at != NOW.replace(tzinfo=None)


async def test_disabled_wallet_cannot_execute_delayed_signal(session):
    """HARDENING #7: approval is rechecked at execution time — a wallet
    disabled during the review delay must not execute (no book request)."""
    trade = await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()

    wallet = await session.get(Wallet, trade.wallet_id)
    wallet.approval_state = "disabled"
    await session.commit()

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("book request must not happen for disabled wallet")

    transport = httpx.MockTransport(boom)
    async with PolymarketClient(
        http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "missed"
    assert order.miss_reason == "wallet_not_approved"
    assert signal.status == "skipped"
    assert await session.scalar(
        select(func.count(Position.id)).where(Position.quantity > 0)
    ) == 0


async def test_fee_aware_buy_sell_round_trip(session, monkeypatch):
    """HARDENING #9: BUY fee enters cost basis, SELL fee exits realized
    P&L — PaperOrder fees and portfolio P&L agree exactly."""
    monkeypatch.setenv("POLYCOPY_MAX_ORDER_SIZE_USD", "20")
    get_settings.cache_clear()

    # BUY: $20 at ask 0.50 → 40 shares, fee $0.20 → cost basis 20.20.
    await _seed_approved_trade(session, address="0xbuyer")
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    book_buy = {"bids": [{"price": "0.48", "size": "500"}],
                "asks": [{"price": "0.50", "size": "100"}]}
    fee_data = {"fd": {"r": "0.02", "e": 1, "to": True}}
    async with _make_client(book_buy, fee_data) as client:
        buy_order = await execute_signal(session, client, signal)
    assert buy_order.status == "filled"
    fee_buy = Decimal(str(buy_order.fee))
    assert fee_buy == Decimal("0.20000")
    position = (await session.execute(select(Position))).scalar_one()
    avg = Decimal(str(position.avg_price))
    qty = Decimal(str(position.quantity))
    assert qty == Decimal("40.000000")
    assert avg == (Decimal("20.20") / 40).quantize(Decimal("0.000001"))

    # SELL all 40 shares at bid 0.50 with a $20 order so the full
    # position exits; fee is another $0.20.
    monkeypatch.setenv("POLYCOPY_MAX_ORDER_SIZE_USD", "20")
    get_settings.cache_clear()
    sell_trade = await _seed_approved_trade(
        session, address="0xseller", side="SELL", price=0.99
    )
    sell_trade.market_id = signal.market_id
    sell_trade.wallet_id = signal.wallet_id
    await session.commit()
    await detect_signals(session, now=NOW)
    sell_signal = (
        await session.execute(select(Signal).where(Signal.side == "SELL"))
    ).scalar_one()
    book_sell = {"bids": [{"price": "0.50", "size": "100"}],
                 "asks": [{"price": "0.50", "size": "100"}]}
    async with _make_client(book_sell, fee_data) as client:
        sell_order = await execute_signal(session, client, sell_signal)

    assert sell_order.status == "filled"
    fee_sell = Decimal(str(sell_order.fee))
    assert fee_sell == Decimal("0.20000")
    await session.refresh(position)
    realized = Decimal(str(position.realized_pnl))
    # Exact: 40 × (0.50 − 0.505) − 0.20 = −0.40.
    assert realized == Decimal("-0.400000")
    assert Decimal(str(position.quantity)) == Decimal("0.000000")
    # PaperOrder fees and portfolio P&L cannot disagree:
    assert (Decimal(20) - fee_sell) - (Decimal(20) + fee_buy) == realized


async def test_malformed_book_fails_closed(session):
    """HARDENING #10: invalid levels are rejected before the walk — no
    division-by-zero, no invalid VWAP, no cycle failure."""
    await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    bad_book = {
        "bids": [{"price": "0", "size": "10"}, {"price": "nan", "size": "5"}],
        "asks": [
            {"price": "0", "size": "10"},          # zero price
            {"price": "-0.5", "size": "10"},       # negative
            {"price": "1.5", "size": "10"},        # above contract max
            {"price": "0.5", "size": "0"},         # zero size
            {"price": "nan", "size": "10"},        # non-finite
            {"price": "inf", "size": "10"},        # non-finite
            {"bogus": "level"},                    # malformed shape
        ],
    }
    async with _make_client(bad_book) as client:
        order = await execute_signal(session, client, signal)
    # Every ask level invalid → no depth → recorded miss, never a crash.
    assert order.status == "missed"
    assert order.miss_reason == "no_book_depth"


async def test_one_failed_signal_does_not_block_later_signals(session):
    """HARDENING #11: a failing CLOB request rolls back THAT signal and
    the cycle continues — later signals still execute."""
    await _seed_approved_trade(session, address="0xbad", asset_id="1111")
    await _seed_approved_trade(session, address="0xgood", asset_id="2222")
    await detect_signals(session, now=NOW)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/clob-markets/"):
            return httpx.Response(200, json={"fd": {"r": 0, "e": 1, "to": True}})
        if request.url.params["token_id"] == "1111":
            # Persistent failure (the client retries transient errors —
            # every attempt for THIS token must fail).
            raise httpx.ConnectError("clob down", request=request)
        return httpx.Response(200, json=BOOK)

    transport = httpx.MockTransport(handler)
    async with PolymarketClient(
        http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        stats = await run_execution_cycle(session, client)

    assert stats["errors"] == 1
    assert stats["filled"] == 1
    # The good signal executed; the bad one is still pending for retry
    # (rollback left no partial order behind).
    statuses = {
        s.source_trade_id: s.status
        for s in (await session.execute(select(Signal))).scalars().all()
    }
    assert sorted(statuses.values()) == ["executed", "pending"]
    assert await session.scalar(select(func.count(PaperOrder.id))) == 1


async def test_execution_backlog_is_bounded(session, monkeypatch):
    """HARDENING #12: one cycle never exceeds execution_batch_size book
    requests, and detection is bounded too."""
    monkeypatch.setenv("POLYCOPY_EXECUTION_BATCH_SIZE", "2")
    monkeypatch.setenv("POLYCOPY_SIGNAL_DETECTION_BATCH_SIZE", "2")
    get_settings.cache_clear()
    for i in range(3):
        await _seed_approved_trade(session, address=f"0x{i}")

    requests = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests["n"] += 1
        if request.url.path.startswith("/clob-markets/"):
            return httpx.Response(200, json={"fd": {"r": 0, "e": 1, "to": True}})
        return httpx.Response(200, json=BOOK)

    transport = httpx.MockTransport(handler)
    async with PolymarketClient(
        http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        stats = await run_execution_cycle(session, client)

    assert stats["signals_created"] == 2  # detection bound
    assert stats["filled"] == 2  # execution bound
    assert requests["n"] == 4  # one book and one fee-info request per fill


async def test_positions_are_isolated_by_source_wallet(session):
    """Final audit: one source wallet's SELL cannot consume another
    source wallet's copied inventory in the same market/outcome."""
    buy_trade = await _seed_approved_trade(session, address="0xwalleta")
    await detect_signals(session, now=NOW)
    buy_signal = (await session.execute(
        select(Signal).where(Signal.wallet_id == buy_trade.wallet_id)
    )).scalar_one()
    async with _make_client() as client:
        await execute_signal(session, client, buy_signal)

    position_a = (await session.execute(
        select(Position).where(Position.wallet_id == buy_trade.wallet_id)
    )).scalar_one()
    qty_before = Decimal(str(position_a.quantity))

    sell_trade = await _seed_approved_trade(
        session, address="0xwalletb", side="SELL", traded_at=NOW
    )
    sell_trade.market_id = buy_signal.market_id
    await session.commit()
    await detect_signals(session, now=NOW)
    sell_signal = (await session.execute(
        select(Signal).where(Signal.wallet_id == sell_trade.wallet_id)
    )).scalar_one()

    async with _make_client() as client:
        order = await execute_signal(session, client, sell_signal)

    assert order.status == "missed"
    assert order.miss_reason == "no_position_to_sell"
    await session.refresh(position_a)
    assert Decimal(str(position_a.quantity)) == qty_before


async def test_run_cycle_records_fresh_t2_per_signal(session, monkeypatch):
    """Final audit: t2 is captured per signal, not once for the batch."""
    await _seed_approved_trade(session, address="0xt2a")
    await _seed_approved_trade(session, address="0xt2b")

    ticks = iter([
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=1, seconds=1),
        NOW + timedelta(minutes=1, seconds=2),
    ])

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    monkeypatch.setattr(execution_service, "datetime", FakeDateTime)
    async with _make_client() as client:
        stats = await run_execution_cycle(session, client)

    assert stats["filled"] == 2
    orders = (await session.execute(
        select(PaperOrder).order_by(PaperOrder.id)
    )).scalars().all()
    assert len(orders) == 2
    assert orders[0].t2_decided_at != orders[1].t2_decided_at


async def test_paper_settlement_winner_loser_idempotent(session):
    """HARDENING #13: open paper positions realize at $1/$0 exactly once
    when the market resolves — repeated cycles are no-ops."""
    from polycopy.execution.service import settle_paper_positions
    from polycopy.models import Settlement

    wallet = Wallet(address="0xs", approval_state="approved",
                    approved_at=NOW - timedelta(hours=1))
    session.add(wallet)
    await session.flush()
    market = Market(condition_id="0xcondsettle", question="?",
                    clob_token_ids={"Up": "4667", "Down": "8761"})
    session.add(market)
    await session.flush()
    # Winner: 10 shares "Up" at avg 0.60 (cost 6.00) → +4.00.
    win = Position(wallet_id=wallet.id, market_id=market.id, outcome="Up", quantity=10,
                   avg_price=Decimal("0.6"), realized_pnl=0, unrealized_pnl=0)
    # Loser: 5 shares "Down" at avg 0.30 (cost 1.50) → −1.50.
    lose = Position(wallet_id=wallet.id, market_id=market.id, outcome="Down", quantity=5,
                    avg_price=Decimal("0.3"), realized_pnl=0, unrealized_pnl=0)
    session.add_all([win, lose])
    session.add(Settlement(market_id=market.id, winning_outcome="Up"))
    await session.commit()

    stats = await settle_paper_positions(session, now=NOW)
    assert stats == {"settled": 2, "winners": 1, "losers": 1}
    await session.refresh(win)
    await session.refresh(lose)
    assert Decimal(str(win.realized_pnl)) == Decimal("4.000000")
    assert Decimal(str(lose.realized_pnl)) == Decimal("-1.500000")
    assert Decimal(str(win.quantity)) == Decimal("0.000000")
    assert Decimal(str(lose.quantity)) == Decimal("0.000000")
    assert win.settled_at is not None and lose.settled_at is not None

    # Idempotent: repeated settlement cycles change nothing.
    stats2 = await settle_paper_positions(session, now=NOW + timedelta(hours=1))
    assert stats2 == {"settled": 0, "winners": 0, "losers": 0}
    await session.refresh(win)
    assert Decimal(str(win.realized_pnl)) == Decimal("4.000000")
