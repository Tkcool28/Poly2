"""Dashboard read endpoints: /wallets, /signals, /positions (PR-F).

These endpoints are the dashboard's only window into the system, so each
payload must carry enough context (market question, wallet address, copy
result, totals) for the UI to explain state in plain language without
extra round trips.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.db import get_db
from polycopy.main import app
from polycopy.models import (
    Base,
    Market,
    PaperOrder,
    Position,
    ServiceHeartbeat,
    Signal,
    Wallet,
    WalletScore,
)


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


async def _seed(session):
    """One approved wallet, one market, one filled signal, one position."""
    wallet = Wallet(
        address="0xdash1",
        label="Sharp bettor",
        approval_state="approved",
        approved_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    market = Market(condition_id="0xmkt1", question="Will it rain in September?")
    session.add_all([wallet, market])
    await session.flush()
    session.add(
        WalletScore(
            wallet_id=wallet.id,
            composite_score=88.0,
            behavioral_tags=[{"verdict": "pending_review"}],
        )
    )
    signal = Signal(
        wallet_id=wallet.id,
        market_id=market.id,
        source_trade_id="t-1",
        asset_id="4667",
        side="BUY",
        outcome="Yes",
        source_price=Decimal("0.450000"),
        status="executed",
        t0_traded_at=datetime(2026, 9, 10, tzinfo=UTC),
        t1_detected_at=datetime(2026, 9, 10, 0, 1, tzinfo=UTC),
    )
    session.add(signal)
    await session.flush()
    session.add(
        PaperOrder(
            idempotency_key="k-1",
            signal_id=signal.id,
            market_id=market.id,
            wallet_id=wallet.id,
            side="BUY",
            size=Decimal("20.000000"),
            price=Decimal("0.500000"),
            status="filled",
            fill_price=Decimal("0.460000"),
            filled_size=Decimal("21.739130"),
            fee=Decimal("0.100000"),
        )
    )
    session.add(
        Position(
            wallet_id=wallet.id,
            market_id=market.id,
            outcome="Yes",
            quantity=Decimal("21.739130"),
            avg_price=Decimal("0.464600"),
            realized_pnl=Decimal("0.000000"),
        )
    )
    await session.commit()
    return wallet, market, signal


async def test_wallets_carry_latest_score_and_timestamps(client, session):
    await _seed(session)
    body = client.get("/wallets").json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["address"] == "0xdash1"
    assert item["label"] == "Sharp bettor"
    assert item["approval_state"] == "approved"
    assert item["composite_score"] == 88.0
    assert item["score_verdict"] == "pending_review"
    assert item["score_computed_at"] is not None
    assert item["approved_at"].startswith("2026-09-01")
    # A wallet with no score reports null, not a crash.
    session.add(Wallet(address="0xnosc"))
    await session.commit()
    items = {i["address"]: i for i in client.get("/wallets").json()["items"]}
    assert items["0xnosc"]["composite_score"] is None
    assert items["0xnosc"]["score_verdict"] is None
    assert items["0xnosc"]["score_computed_at"] is None


async def test_signals_carry_context_and_copy_result(client, session):
    await _seed(session)
    body = client.get("/signals").json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["market_question"] == "Will it rain in September?"
    assert item["wallet_address"] == "0xdash1"
    assert item["side"] == "BUY"
    assert item["outcome"] == "Yes"
    assert item["t0_traded_at"].startswith("2026-09-10")
    assert item["t1_detected_at"] > item["t0_traded_at"]
    order = item["paper_order"]
    assert order["status"] == "filled"
    assert order["fill_price"] == pytest.approx(0.46)
    assert order["fee"] == pytest.approx(0.10)
    assert item["source_trade_id"] == "t-1"
    assert item["eligible_at"] is not None
    assert item["detection_lag_seconds"] == 60
    assert item["kill_switch_deferrals"] == 0


async def test_paper_evidence_api_exposes_copy_metrics_without_inventing_source_pnl(
    client, session
):
    await _seed(session)
    result = client.get("/paper/evidence")
    assert result.status_code == 200
    row = result.json()["items"][0]
    assert row["signals_generated"] == 1
    assert row["copied_trades"] == 1
    assert row["median_detection_lag_seconds"] == 60
    assert row["median_adverse_slippage"] == pytest.approx(0.01)
    assert row["source_wallet_performance_comparison"]["available"] is False
    backlog = client.get("/paper/backlog").json()
    assert backlog["pending"] == 0
    assert backlog["kill_switch_enabled"] is True


async def test_signal_without_order_reports_null_copy_result(client, session):
    wallet, market, _ = await _seed(session)
    session.add(
        Signal(
            wallet_id=wallet.id,
            market_id=market.id,
            source_trade_id="t-2",
            side="SELL",
            outcome="Yes",
            source_price=Decimal("0.500000"),
            status="pending",
            t0_traded_at=datetime(2026, 9, 11, tzinfo=UTC),
            t1_detected_at=datetime(2026, 9, 11, 0, 1, tzinfo=UTC),
        )
    )
    await session.commit()
    items = client.get("/signals").json()["items"]
    pending = next(i for i in items if i["status"] == "pending")
    assert pending["paper_order"] is None


async def test_positions_carry_context_and_totals(client, session):
    await _seed(session)
    body = client.get("/positions").json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["market_question"] == "Will it rain in September?"
    assert item["wallet_address"] == "0xdash1"
    assert item["outcome"] == "Yes"
    assert item["quantity"] == pytest.approx(21.73913)
    assert item["settled_at"] is None
    totals = body["totals"]
    assert totals["open_count"] == 1
    assert totals["settled_count"] == 0
    assert totals["open_cost_usd"] == pytest.approx(21.739130 * 0.4646, rel=1e-4)
    assert totals["realized_pnl_usd"] == pytest.approx(0.0)


async def test_settled_position_moves_out_of_open_totals(client, session):
    wallet, market, _ = await _seed(session)
    session.add(
        Position(
            wallet_id=wallet.id,
            market_id=market.id,
            outcome="No",
            quantity=Decimal(0),
            avg_price=Decimal("0.400000"),
            realized_pnl=Decimal("4.000000"),
            settled_at=datetime(2026, 9, 20, tzinfo=UTC),
        )
    )
    await session.commit()
    body = client.get("/positions").json()
    totals = body["totals"]
    assert totals["open_count"] == 1
    assert totals["settled_count"] == 1
    assert totals["realized_pnl_usd"] == pytest.approx(4.0)


class _FakeRedis:
    async def ping(self):
        return True

    async def aclose(self):
        return None


async def test_health_deps_reports_missing_bot_heartbeat(client, monkeypatch):
    import polycopy.main as main_module

    monkeypatch.setattr(
        main_module.aioredis,
        "from_url",
        lambda *args, **kwargs: _FakeRedis(),
    )
    body = client.get("/health/deps").json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["bot"] == "missing"


async def test_health_deps_distinguishes_alive_from_failing_cycle(
    client, session, monkeypatch
):
    import polycopy.main as main_module

    monkeypatch.setattr(
        main_module.aioredis,
        "from_url",
        lambda *args, **kwargs: _FakeRedis(),
    )

    now = datetime.now(UTC)
    session.add_all(
        [
            ServiceHeartbeat(service="bot_alive", seen_at=now),
            ServiceHeartbeat(service="bot_success", seen_at=now - timedelta(minutes=2)),
            ServiceHeartbeat(service="bot_failure", seen_at=now),
        ]
    )
    await session.commit()

    body = client.get("/health/deps").json()
    assert body["status"] == "degraded"
    assert body["checks"]["bot"] == "ok"
    assert body["checks"]["bot_cycle"] == "failing"
    assert body["heartbeat"]["process_seen_at"] is not None
    assert body["heartbeat"]["last_success_at"] is not None
    assert body["heartbeat"]["last_failure_at"] is not None


async def test_health_deps_reports_clean_then_stale_cycle(client, session, monkeypatch):
    import polycopy.main as main_module

    monkeypatch.setattr(
        main_module.aioredis,
        "from_url",
        lambda *args, **kwargs: _FakeRedis(),
    )

    now = datetime.now(UTC)
    alive = ServiceHeartbeat(service="bot_alive", seen_at=now)
    success = ServiceHeartbeat(service="bot_success", seen_at=now)
    session.add_all([alive, success])
    await session.commit()

    body = client.get("/health/deps").json()
    assert body["status"] == "ok"
    assert body["checks"]["bot"] == "ok"
    assert body["checks"]["bot_cycle"] == "ok"

    success.seen_at = now - timedelta(minutes=5)
    await session.commit()
    body = client.get("/health/deps").json()
    assert body["status"] == "degraded"
    assert body["checks"]["bot"] == "ok"
    assert body["checks"]["bot_cycle"] == "stale"


async def test_position_totals_are_not_limited_to_display_window(client, session):
    wallet = Wallet(address="0xmany", approval_state="approved")
    market = Market(condition_id="0xmanymarket", question="Many positions?")
    session.add_all([wallet, market])
    await session.flush()
    session.add_all(
        [
            Position(
                wallet_id=wallet.id,
                market_id=market.id,
                outcome=f"O{i}",
                quantity=Decimal(0),
                avg_price=Decimal("0.500000"),
                realized_pnl=Decimal("1.000000"),
            )
            for i in range(501)
        ]
    )
    await session.commit()

    body = client.get("/positions").json()
    assert body["count"] == 500
    assert body["totals"]["realized_pnl_usd"] == pytest.approx(501.0)


async def test_wallet_latest_score_is_per_wallet_not_global_history_limit(client, session):
    noisy = Wallet(address="0xnoisy", approval_state="discovered")
    quiet = Wallet(address="0xquiet", approval_state="approved")
    session.add_all([noisy, quiet])
    await session.flush()

    base = datetime(2026, 9, 1, tzinfo=UTC)
    session.add(
        WalletScore(
            wallet_id=quiet.id,
            composite_score=91.0,
            behavioral_tags=[],
            computed_at=base,
        )
    )
    session.add_all(
        [
            WalletScore(
                wallet_id=noisy.id,
                composite_score=float(i % 100),
                behavioral_tags=[],
                computed_at=base + timedelta(seconds=i + 1),
            )
            for i in range(2001)
        ]
    )
    await session.commit()

    items = {i["address"]: i for i in client.get("/wallets").json()["items"]}
    assert items["0xquiet"]["composite_score"] == pytest.approx(91.0)
    assert items["0xnoisy"]["composite_score"] == pytest.approx(0.0)


async def test_wallet_insufficient_history_is_distinct_from_never_scored(client, session):
    insufficient = Wallet(address="0xinsufficient", approval_state="discovered")
    never = Wallet(address="0xnever", approval_state="discovered")
    session.add_all([insufficient, never])
    await session.flush()
    session.add(
        WalletScore(
            wallet_id=insufficient.id,
            composite_score=None,
            behavioral_tags=[
                {
                    "verdict": "insufficient_history",
                    "gate_failures": ["trades 0 < 30"],
                }
            ],
        )
    )
    await session.commit()

    items = {item["address"]: item for item in client.get("/wallets").json()["items"]}
    assert items["0xinsufficient"]["composite_score"] is None
    assert items["0xinsufficient"]["score_verdict"] == "insufficient_history"
    assert items["0xinsufficient"]["score_computed_at"] is not None
    assert items["0xnever"]["score_verdict"] is None
    assert items["0xnever"]["score_computed_at"] is None
