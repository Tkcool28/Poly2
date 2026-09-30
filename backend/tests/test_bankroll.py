"""Paper bankroll: cash awareness, profit sweeps, stop-loss floor, ledger."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.bankroll import (
    get_bankroll_account,
    open_position_cost,
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
    BankrollAccount,
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


async def _seed_open_position(session, *, quantity, avg_price) -> Position:
    """An open paper position tying up ``quantity × avg_price`` of cash."""
    trade = await _seed_approved_trade(session)
    position = Position(
        wallet_id=trade.wallet_id,
        market_id=trade.market_id,
        outcome="Up",
        quantity=quantity,
        avg_price=avg_price,
    )
    session.add(position)
    await session.commit()
    return position


# --- Account + ledger basics -------------------------------------------------


async def test_account_auto_created_with_settings_defaults(session):
    account = await get_bankroll_account(session)
    assert Decimal(str(account.starting_bankroll_usd)) == Decimal(200)
    assert account.balance == Decimal(200)
    assert Decimal(str(account.profit_limit_usd)) == Decimal(50)
    assert Decimal(str(account.stop_loss_floor_usd)) == Decimal(100)
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
    assert await post_realized_pnl(session, Decimal(0), context={}) is None
    assert (await session.scalar(select(BankrollLedgerEntry.id))) is None


# --- Profit sweep -------------------------------------------------------------


async def test_sweep_moves_profit_out_and_returns_to_base(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(60), context={"kind": "settlement"})
    await session.commit()

    swept = await sweep_profit_if_due(session)
    await session.commit()

    assert swept == Decimal(60)
    assert Decimal(str(account.withdrawn_total)) == Decimal(60)
    # Bankroll returns to its base after cashing out — profit now "yours".
    assert account.balance == Decimal(200)
    entries = (
        await session.execute(
            select(BankrollLedgerEntry).order_by(BankrollLedgerEntry.id)
        )
    ).scalars().all()
    assert [e.entry_type for e in entries] == ["realized_pnl", "profit_withdrawal"]
    assert Decimal(str(entries[1].amount)) == Decimal(-60)
    log = (await session.execute(select(DecisionLogEntry))).scalar_one()
    assert log.action == "profit_withdrawn"
    # Not due again until NEW profit accumulates past the limit.
    assert await sweep_profit_if_due(session) == 0


async def test_losses_after_sweep_are_never_redeposited(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(60), context={})
    await sweep_profit_if_due(session)
    await session.commit()
    # Losing streak after the withdrawal: balance drops, nothing to sweep.
    await post_realized_pnl(session, Decimal(-3), context={})
    await session.commit()
    assert account.balance == Decimal(197)
    assert Decimal(str(account.withdrawn_total)) == Decimal(60)
    assert await sweep_profit_if_due(session) == 0


async def test_sweep_disabled_when_limit_zero(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_PROFIT_LIMIT_USD", "0")
    get_settings.cache_clear()
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(80), context={})
    await session.commit()
    assert await sweep_profit_if_due(session) == 0
    assert account.balance == Decimal(280)


async def test_sweep_waits_until_limit_reached(session):
    account = await get_bankroll_account(session)  # limit $50
    await post_realized_pnl(session, Decimal(30), context={})
    await session.commit()
    assert await sweep_profit_if_due(session) == 0
    await post_realized_pnl(session, Decimal(25), context={})
    await session.commit()
    assert await sweep_profit_if_due(session) == Decimal(55)
    assert account.balance == Decimal(200)


# --- Execution gates ------------------------------------------------------------


async def test_buy_missed_when_bankroll_cash_insufficient(session, monkeypatch):
    monkeypatch.setenv("POLYCOPY_MAX_EXPOSURE_GLOBAL_USD", "100000")
    monkeypatch.setenv("POLYCOPY_MAX_EXPOSURE_PER_MARKET_USD", "100000")
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
    monkeypatch.setenv("POLYCOPY_MAX_EXPOSURE_PER_MARKET_USD", "100000")
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
    await post_realized_pnl(session, Decimal(-105), context={})
    await session.commit()
    assert account.balance == Decimal(95)
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

    assert sell_order.status == "partial"  # capped at owned qty, target unmet
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

    # Capped at the 5 owned shares, so the $10 target is unmet → partial;
    # all 5 shares still sell and the realized P&L posts.
    assert order.status == "partial"
    account = await get_bankroll_account(session)
    # 5 × (0.48 bid − 0.40 avg), zero fee → +0.40.
    assert Decimal(str(account.realized_pnl_total)) == Decimal("0.4")
    assert account.balance == Decimal("200.4")


# --- Cycle wiring ---------------------------------------------------------------


async def test_execution_cycle_runs_profit_sweep(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(60), context={})
    await session.commit()

    async with _make_client() as client:
        await run_execution_cycle(session, client)

    assert Decimal(str(account.withdrawn_total)) == Decimal(60)
    assert account.balance == Decimal(200)


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
    assert Decimal(str(account.profit_limit_usd)) == Decimal(25)
    assert Decimal(str(account.stop_loss_floor_usd)) == Decimal(0)

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


# --- Sweep cash-availability bound ---------------------------------------------


async def test_sweep_is_bounded_by_free_cash(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(60), context={"kind": "settlement"})
    await session.commit()
    # $230 of the $260 balance is deployed → only $30 is actually free.
    await _seed_open_position(session, quantity=230, avg_price=1)

    swept = await sweep_profit_if_due(session)
    await session.commit()

    assert swept == Decimal(30)
    assert Decimal(str(account.withdrawn_total)) == Decimal(30)
    assert account.sweep_pending is True
    # The invariant: a withdrawal can never push available cash negative.
    available = account.balance - await open_position_cost(session)
    assert available >= 0
    withdrawal = (
        await session.execute(
            select(BankrollLedgerEntry).where(
                BankrollLedgerEntry.entry_type == "profit_withdrawal"
            )
        )
    ).scalar_one()
    assert Decimal(str(withdrawal.amount)) == Decimal(-30)
    assert withdrawal.context["partial"] is True


async def test_sweep_zero_when_all_cash_is_deployed(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(60), context={})
    await session.commit()
    # Every dollar is inside open positions → nothing can leave.
    await _seed_open_position(session, quantity=260, avg_price=1)

    assert await sweep_profit_if_due(session) == 0
    await session.commit()

    assert Decimal(str(account.withdrawn_total)) == 0
    assert account.sweep_pending is False
    # Idempotent: further cycles move nothing while cash stays deployed.
    assert await sweep_profit_if_due(session) == 0
    withdrawals = (
        await session.execute(
            select(BankrollLedgerEntry).where(
                BankrollLedgerEntry.entry_type == "profit_withdrawal"
            )
        )
    ).scalars().all()
    assert withdrawals == []


async def test_full_sweep_when_no_cash_is_deployed(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(60), context={})
    await session.commit()

    swept = await sweep_profit_if_due(session)
    await session.commit()

    assert swept == Decimal(60)
    assert account.balance == Decimal(200)
    assert account.sweep_pending is False


async def test_partial_sweep_completes_when_cash_frees_up(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(60), context={})
    await session.commit()
    position = await _seed_open_position(session, quantity=230, avg_price=1)

    first = await sweep_profit_if_due(session)
    await session.commit()
    assert first == Decimal(30)
    assert account.sweep_pending is True

    # Cash still tied up: nothing more moves, no duplicate withdrawal.
    assert await sweep_profit_if_due(session) == 0

    # Positions close → the $30 remainder becomes sweepable even though
    # it never re-crossed the $50 limit on its own.
    position.quantity = 0
    position.settled_at = NOW
    await session.commit()

    second = await sweep_profit_if_due(session)
    await session.commit()
    assert second == Decimal(30)
    assert Decimal(str(account.withdrawn_total)) == Decimal(60)
    assert account.balance == Decimal(200)
    assert account.sweep_pending is False

    # Done: no third withdrawal, ever.
    assert await sweep_profit_if_due(session) == 0
    withdrawals = (
        await session.execute(
            select(BankrollLedgerEntry).where(
                BankrollLedgerEntry.entry_type == "profit_withdrawal"
            )
        )
    ).scalars().all()
    assert len(withdrawals) == 2


async def test_losses_cancel_a_pending_sweep(session):
    account = await get_bankroll_account(session)
    await post_realized_pnl(session, Decimal(60), context={})
    await session.commit()
    await _seed_open_position(session, quantity=230, avg_price=1)
    assert await sweep_profit_if_due(session) == Decimal(30)
    await session.commit()
    # Losing streak erases the remaining pile before cash frees up.
    await post_realized_pnl(session, Decimal(-40), context={})
    await session.commit()
    assert await sweep_profit_if_due(session) == 0
    assert account.sweep_pending is False
    # New small profit must re-cross the limit — no stale pending sweep.
    position = (
        await session.execute(select(Position).where(Position.outcome == "Up"))
    ).scalar_one()
    position.quantity = 0
    position.settled_at = NOW
    await post_realized_pnl(session, Decimal(10), context={})
    await session.commit()
    assert await sweep_profit_if_due(session) == 0


# --- Account persistence (API boundary) -----------------------------------------

async def _get_bankroll_with_engine(engine):
    """GET /bankroll with production-style per-request sessions."""
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with maker() as request_session:
            yield request_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            return test_client.get("/bankroll")
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def fresh_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_get_bankroll_persists_account_on_fresh_database(fresh_engine):
    response = await _get_bankroll_with_engine(fresh_engine)
    assert response.status_code == 200
    # Proof of COMMIT, not just flush: a brand-new session must see the row.
    maker = async_sessionmaker(fresh_engine, expire_on_commit=False)
    async with maker() as check:
        count = await check.scalar(select(func.count()).select_from(BankrollAccount))
    assert count == 1


async def test_get_bankroll_second_read_returns_same_persisted_values(fresh_engine):
    first = (await _get_bankroll_with_engine(fresh_engine)).json()
    maker = async_sessionmaker(fresh_engine, expire_on_commit=False)
    async with maker() as write:
        account = await get_bankroll_account(write)
        account.realized_pnl_total = Decimal(10)
        await write.commit()

    second = (await _get_bankroll_with_engine(fresh_engine)).json()
    third = (await _get_bankroll_with_engine(fresh_engine)).json()
    assert second == third
    assert second["starting_bankroll_usd"] == first["starting_bankroll_usd"]
    assert second["realized_pnl_total_usd"] == 10.0


async def test_env_defaults_do_not_reseed_existing_account(fresh_engine, monkeypatch):
    first = (await _get_bankroll_with_engine(fresh_engine)).json()
    assert first["starting_bankroll_usd"] == 200.0

    monkeypatch.setenv("POLYCOPY_STARTING_BANKROLL_USD", "999")
    monkeypatch.setenv("POLYCOPY_PROFIT_LIMIT_USD", "500")
    get_settings.cache_clear()

    second = (await _get_bankroll_with_engine(fresh_engine)).json()
    # DB wins over env after creation — no silent reseed or replacement.
    assert second["starting_bankroll_usd"] == 200.0
    assert second["profit_limit_usd"] == 50.0


async def test_repeated_reads_create_exactly_one_account_row(fresh_engine):
    for _ in range(3):
        response = await _get_bankroll_with_engine(fresh_engine)
        assert response.status_code == 200
    maker = async_sessionmaker(fresh_engine, expire_on_commit=False)
    async with maker() as check:
        count = await check.scalar(select(func.count()).select_from(BankrollAccount))
    assert count == 1
