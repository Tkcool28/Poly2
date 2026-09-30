"""Paper bankroll: cash awareness, profit sweeps, stop-loss floor, ledger."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.bankroll import (
    get_bankroll_account,
    post_realized_pnl,
    stop_loss_hit,
    sweep_profit_if_due,
)
from polycopy.config import get_settings
from polycopy.db import get_db
from polycopy.execution.service import (
    detect_signals,
    execute_signal,
    run_execution_cycle,
)
from polycopy.ingestion.client import PolymarketClient
from polycopy.main import app
from polycopy.models import (
    BankrollLedgerEntry,
    Base,
    DecisionLogEntry,
    Market,
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
def _settings(monkeypatch):
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")
    monkeypatch.setenv("POLYCOPY_MAX_SIGNAL_EXECUTION_AGE_SECONDS", "10000000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def client(session):
    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_client(book: dict | None = None) -> PolymarketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "clob.polymarket.com"
        if request.url.path.startswith("/clob-markets/"):
            return httpx.Response(200, json={"fd": {"r": 0, "e": 1, "to": True}})
        assert request.url.path == "/book"
        return httpx.Response(200, json=book or BOOK)

    transport = httpx.MockTransport(handler)
    return PolymarketClient(http_client=httpx.AsyncClient(transport=transport))


async def _seed_approved_trade(
    session,
    *,
    address="0xsmart",
    side="BUY",
    outcome="Up",
    price=0.45,
) -> Trade:
    wallet = Wallet(
        address=address,
        approval_state="approved",
        approved_at=NOW - timedelta(hours=1),
    )
    session.add(wallet)
    await session.flush()
    market = Market(
        condition_id=f"0xcond{address}",
        question="?",
        clob_token_ids={"Up": "4667", "Down": "8761"},
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
        ingested_at=NOW,
    )
    session.add(trade)
    await session.commit()
    return trade


# --- Account + ledger basics -------------------------------------------------


async def test_account_auto_created_with_settings_defaults(session):
    account = await get_bankroll_account(session)
    assert Decimal(str(account.starting_bankroll_usd)) == Decimal("200")
    assert account.balance == Decimal("200")
    assert Decimal(str(account.profit_limit_usd)) == Decimal("50")
    assert Decimal(str(account.stop_loss_floor_usd)) == Decimal("100")
    # get-or-create is stable: same row on repeat calls.
    assert (await get_bankroll_account(session)).id == account.id


async def test_realized_pnl_posts_to_ledger_and_moves_balance(session):
    await get_bankroll_account(session)
    entry = await post_realized_pnl(
        session, Decimal("-0.4"), context={"kind": "paper_sell"}
    )
    await session.commit()
    account = await get_bankroll_account(session)
    assert Decimal(str(account.realized_pnl_total)) == Decimal("-0.4")
    assert account.balance == Decimal("199.6")
    assert entry is not None
    assert entry.entry_type == "realized_pnl"
    assert Decimal(str(entry.amount)) == Decimal("-0.4")
    assert Decimal(str(entry.balance_after)) == Decimal("199.6")


async def test_zero_realized_pnl_writes_no_ledger_noise(session):
    await get_bankroll_account(session)
    assert await post_realized_pnl(session, Decimal("0"), context={}) is None
    assert (await session.scalar(select(BankrollLedgerEntry.id))) is None


# --- Profit sweep -------------------------------------------------------------


async def test_sweep_moves_profit_out_and_returns_to_base(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal("60"), context={"kind": "settlement"})
    await session.commit()

    swept = await sweep_profit_if_due(session)
    await session.commit()

    assert swept == Decimal("60")
    assert Decimal(str(account.withdrawn_total)) == Decimal("60")
    # Bankroll returns to its base after cashing out — profit now "yours".
    assert account.balance == Decimal("200")
    entries = (
        await session.execute(
            select(BankrollLedgerEntry).order_by(BankrollLedgerEntry.id)
        )
    ).scalars().all()
    assert [e.entry_type for e in entries] == ["realized_pnl", "profit_withdrawal"]
    assert Decimal(str(entries[1].amount)) == Decimal("-60")
    log = (await session.execute(select(DecisionLogEntry))).scalar_one()
    assert log.action == "profit_withdrawn"
    # Not due again until NEW profit accumulates past the limit.
    assert await sweep_profit_if_due(session) == 0


async def test_losses_after_sweep_are_never_redeposited(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal("60"), context={})
    await sweep_profit_if_due(session)
    await session.commit()
    # Losing streak after the withdrawal: balance drops, nothing to sweep.
    await post_realized_pnl(session, Decimal("-3"), context={})
    await session.commit()
    assert account.balance == Decimal("197")
    assert Decimal(str(account.withdrawn_total)) == Decimal("60")
    assert await sweep_profit_if_due(session) == 0


async def test_sweep_disabled_when_limit_zero(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_PROFIT_LIMIT_USD", "0")
    get_settings.cache_clear()
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal("80"), context={})
    await session.commit()
    assert await sweep_profit_if_due(session) == 0
    assert account.balance == Decimal("280")


async def test_sweep_waits_until_limit_reached(session):
    account = await get_bankroll_account(session)  # limit $50
    await post_realized_pnl(session, Decimal("30"), context={})
    await session.commit()
    assert await sweep_profit_if_due(session) == 0
    await post_realized_pnl(session, Decimal("25"), context={})
    await session.commit()
    assert await sweep_profit_if_due(session) == Decimal("55")
    assert account.balance == Decimal("200")


# --- Execution gates ------------------------------------------------------------


async def test_buy_missed_when_bankroll_cash_insufficient(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_MAX_EXPOSURE_GLOBAL_USD", "100000")
    get_settings.cache_clear()
    trade = await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    # $196 already deployed → $4 free cash; the $10 fill cannot fit.
    session.add(
        Position(
            wallet_id=trade.wallet_id,
            market_id=trade.market_id,
            outcome="Down",
            quantity=196,
            avg_price=1,
        )
    )
    await session.commit()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "missed"
    assert order.miss_reason == "bankroll_insufficient"
    await session.refresh(signal)
    assert signal.status == "skipped"


async def test_partial_fill_fits_where_full_fill_would_not(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_MAX_EXPOSURE_GLOBAL_USD", "100000")
    get_settings.cache_clear()
    trade = await _seed_approved_trade(session)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    # Only $6 free: the book offers 8 shares @ $0.50 = $4 notional → fills.
    session.add(
        Position(
            wallet_id=trade.wallet_id,
            market_id=trade.market_id,
            outcome="Down",
            quantity=194,
            avg_price=1,
        )
    )
    await session.commit()

    shallow = {"bids": [], "asks": [{"price": "0.50", "size": "8"}]}
    async with _make_client(shallow) as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "partial"
    assert order.filled_size == 8

    # Tighter cash ($3): the same $4 fill no longer fits → recorded miss.
    trade2 = await _seed_approved_trade(session, address="0xsmart2")
    await detect_signals(session, now=NOW)
    signal2 = (
        await session.execute(select(Signal).where(Signal.wallet_id == trade2.wallet_id))
    ).scalar_one()
    session.add(
        Position(
            wallet_id=trade2.wallet_id,
            market_id=trade2.market_id,
            outcome="Down",
            quantity=197,
            avg_price=1,
        )
    )
    await session.commit()

    async with _make_client(shallow) as client:
        order2 = await execute_signal(session, client, signal2)

    assert order2.status == "missed"
    assert order2.miss_reason == "bankroll_insufficient"


async def test_stop_loss_floor_halts_new_buys_but_sells_still_run(
    session, monkeypatch
):
    monkeypatch.setenv("POLYCOPY_MAX_EXPOSURE_GLOBAL_USD", "100000")
    get_settings.cache_clear()
    account = await get_bankroll_account(session)  # floor $100
    await post_realized_pnl(session, Decimal("-105"), context={})
    await session.commit()
    assert account.balance == Decimal("95")
    assert stop_loss_hit(account)

    # New BUY: halted at the floor — recorded miss.
    await _seed_approved_trade(session, address="0xhalt")
    await detect_signals(session, now=NOW)
    buy_signal = (await session.execute(select(Signal))).scalar_one()

    async with _make_client() as client:
        order = await execute_signal(session, client, buy_signal)

    assert order.status == "missed"
    assert order.miss_reason == "bankroll_stop_loss"

    # A SELL still executes: it returns cash and realizes the exit.
    sell_trade = await _seed_approved_trade(
        session, address="0xexit", side="SELL", price=0.50
    )
    sell_trade.market_id = buy_signal.market_id
    sell_trade.wallet_id = buy_signal.wallet_id
    await session.commit()
    await detect_signals(session, now=NOW)
    sell_signal = (
        await session.execute(select(Signal).where(Signal.side == "SELL"))
    ).scalar_one()
    session.add(
        Position(
            wallet_id=sell_signal.wallet_id,
            market_id=sell_signal.market_id,
            outcome="Up",
            quantity=10,
            avg_price=0.40,
        )
    )
    await session.commit()

    async with _make_client() as client:
        sell_order = await execute_signal(session, client, sell_signal)

    assert sell_order.status == "filled"
    # SELL realized P&L reached the bankroll ledger anyway.
    entries = (
        await session.execute(
            select(BankrollLedgerEntry).where(
                BankrollLedgerEntry.entry_type == "realized_pnl"
            )
        )
    ).scalars().all()
    assert Decimal(str(entries[-1].amount)) > 0


async def test_sell_posts_realized_pnl_to_bankroll(session):
    await _seed_approved_trade(session, side="SELL", price=0.50)
    await detect_signals(session, now=NOW)
    signal = (await session.execute(select(Signal))).scalar_one()
    session.add(
        Position(
            wallet_id=signal.wallet_id,
            market_id=signal.market_id,
            outcome="Up",
            quantity=5,
            avg_price=0.40,
        )
    )
    await session.commit()

    async with _make_client() as client:
        order = await execute_signal(session, client, signal)

    assert order.status == "filled"
    account = await get_bankroll_account(session)
    # 5 × (0.48 bid − 0.40 avg), zero fee → +0.40.
    assert Decimal(str(account.realized_pnl_total)) == Decimal("0.4")
    assert account.balance == Decimal("200.4")


# --- Cycle wiring ---------------------------------------------------------------


async def test_execution_cycle_runs_profit_sweep(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal("60"), context={})
    await session.commit()

    async with _make_client() as client:
        await run_execution_cycle(session, client)

    assert Decimal(str(account.withdrawn_total)) == Decimal("60")
    assert account.balance == Decimal("200")


async def test_cycle_stats_shape_unchanged_by_sweep(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_PROFIT_LIMIT_USD", "0.1")
    get_settings.cache_clear()
    await _seed_approved_trade(session)
    async with _make_client() as client:
        stats = await run_execution_cycle(session, client)
    assert stats == {
        "signals_created": 1, "filled": 1, "partial": 0, "missed": 0,
        "deferred": 0, "errors": 0, "positions_settled": 0,
    }


# --- API ------------------------------------------------------------------------


async def test_bankroll_overview_endpoint(client):
    resp = client.get("/bankroll")
    assert resp.status_code == 200
    body = resp.json()
    assert body["starting_bankroll_usd"] == 200
    assert body["bankroll_balance_usd"] == 200
    assert body["profit_limit_usd"] == 50
    assert body["stop_loss_floor_usd"] == 100
    assert body["available_cash_usd"] == 200
    assert body["stop_loss_hit"] is False
    assert body["withdrawn_total_usd"] == 0


async def test_ledger_endpoint_lists_movements(client, session):
    await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal("-0.4"), context={"kind": "paper_sell"})
    await session.commit()
    resp = client.get("/bankroll/ledger")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["entry_type"] == "realized_pnl"
    assert body["items"][0]["amount"] == pytest.approx(-0.4)
    assert body["items"][0]["balance_after"] == pytest.approx(199.6)


async def test_settings_endpoint_updates_and_writes_decision_log(client, session):
    resp = client.post(
        "/bankroll/settings",
        json={"profit_limit_usd": 25, "stop_loss_floor_usd": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profit_limit_usd"] == 25
    assert body["stop_loss_floor_usd"] == 0

    account = await get_bankroll_account(session)
    assert Decimal(str(account.profit_limit_usd)) == Decimal("25")
    assert Decimal(str(account.stop_loss_floor_usd)) == Decimal("0")

    logs = (await session.execute(select(DecisionLogEntry))).scalars().all()
    assert any(log.action == "bankroll_settings_updated" for log in logs)

    # Omitted fields keep their values (0 is a real value, not "unset").
    resp = client.post("/bankroll/settings", json={"profit_limit_usd": 10})
    assert resp.json()["stop_loss_floor_usd"] == 0


async def test_settings_endpoint_rejects_negative_limits(client):
    assert client.post(
        "/bankroll/settings", json={"profit_limit_usd": -5}
    ).status_code == 422
    assert client.post(
        "/bankroll/settings", json={"stop_loss_floor_usd": -1}
    ).status_code == 422
