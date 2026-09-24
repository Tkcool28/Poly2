"""Approval queue endpoints: the human side of the state machine."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.db import get_db
from polycopy.main import app
from polycopy.models import ApprovalQueueEntry, Base, DecisionLogEntry, Wallet, WalletScore


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


async def _queue_wallet(session, address="0xwallet1") -> Wallet:
    wallet = Wallet(address=address, approval_state="pending_review")
    session.add(wallet)
    await session.flush()
    session.add(ApprovalQueueEntry(wallet_id=wallet.id, state="pending"))
    session.add(
        WalletScore(
            wallet_id=wallet.id,
            window_days=90,
            composite_score=82.5,
            profit_factor=9.23,
            profit_factor_90d=9.02,
            gross_profit_90d=39564.746026,
            gross_loss_90d=4386.018309,
            realized_pnl_90d=35178.727718,
            behavioral_tags=[{"version": "v1", "components": {"pnl_quality": 80.0}}],
        )
    )
    await session.commit()
    return wallet


async def test_approval_queue_lists_pending_with_score(client, session):
    await _queue_wallet(session)
    resp = client.get("/approval-queue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["address"] == "0xwallet1"
    assert item["score"]["composite_score"] == 82.5
    assert item["score"]["profit_factor"] == pytest.approx(9.23)
    assert item["score"]["profit_factor_90d"] == pytest.approx(9.02)
    assert item["score"]["gross_profit_90d"] == pytest.approx(39564.746026)
    assert item["score"]["gross_loss_90d"] == pytest.approx(4386.018309)
    assert item["score"]["realized_pnl_90d"] == pytest.approx(35178.727718)
    assert item["score"]["window_days"] == 90
    assert item["score"]["breakdown"]["components"]["pnl_quality"] == 80.0

    # Repeated reads are deterministic and do not recompute a different window.
    assert client.get("/approval-queue").json() == body


async def test_get_wallet_score_404_when_unscored(client, session):
    wallet = Wallet(address="0xnosc", approval_state="discovered")
    session.add(wallet)
    await session.commit()
    assert client.get(f"/wallets/{wallet.id}/score").status_code == 404


async def test_approve_transitions_and_closes_entry(client, session):
    wallet = await _queue_wallet(session)
    resp = client.post(f"/wallets/{wallet.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["approval_state"] == "approved"
    await session.refresh(wallet)
    assert wallet.approval_state == "approved"
    entry = (await session.execute(select(ApprovalQueueEntry))).scalar_one()
    assert entry.state == "approved"
    assert entry.reviewer == "human:api"
    logs = (await session.execute(select(DecisionLogEntry))).scalars().all()
    assert any("approve" in log.action for log in logs)


async def test_reject_from_pending_review(client, session):
    wallet = await _queue_wallet(session)
    resp = client.post(f"/wallets/{wallet.id}/reject")
    assert resp.status_code == 200
    await session.refresh(wallet)
    assert wallet.approval_state == "rejected"


async def test_approve_from_discovered_is_409(client, session):
    wallet = Wallet(address="0xdisc", approval_state="discovered")
    session.add(wallet)
    await session.commit()
    resp = client.post(f"/wallets/{wallet.id}/approve")
    assert resp.status_code == 409
    assert "pending_review" in resp.json()["detail"]


async def test_disable_requires_approved(client, session):
    wallet = await _queue_wallet(session)
    assert client.post(f"/wallets/{wallet.id}/disable").status_code == 409
    client.post(f"/wallets/{wallet.id}/approve")
    resp = client.post(f"/wallets/{wallet.id}/disable")
    assert resp.status_code == 200
    await session.refresh(wallet)
    assert wallet.approval_state == "disabled"


async def test_unknown_action_and_wallet(client, session):
    wallet = await _queue_wallet(session)
    assert client.post(f"/wallets/{wallet.id}/explode").status_code == 404
    assert client.post("/wallets/99999/approve").status_code == 404


async def test_conflicting_concurrent_decisions_exactly_one_succeeds():
    """REVIEW REGRESSION: two operators acting on the same stale view.

    Both sessions observe the wallet as pending_review (the old race
    window in the read-then-write version), then both submit a decision.
    The transition is a conditional UPDATE (source state in the WHERE
    clause), so exactly one succeeds and the other gets 409.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as seed:
        wallet = Wallet(address="0xracy", approval_state="pending_review")
        seed.add(wallet)
        await seed.flush()
        seed.add(ApprovalQueueEntry(wallet_id=wallet.id, state="pending"))
        await seed.commit()
        wallet_id = wallet.id

    s1 = maker()
    s2 = maker()
    # Both observe the pre-decision state — this is the race window.
    assert (await s1.get(Wallet, wallet_id)).approval_state == "pending_review"
    assert (await s2.get(Wallet, wallet_id)).approval_state == "pending_review"

    responses = []
    for s in (s1, s2):
        # Factory, not a default arg: FastAPI inspects override signatures
        # and would try to deepcopy an AsyncSession default.
        def make_override(session=s):
            async def override_get_db():
                yield session
            return override_get_db
        app.dependency_overrides[get_db] = make_override()
        with TestClient(app) as c:
            responses.append(c.post(f"/wallets/{wallet_id}/approve"))
    app.dependency_overrides.clear()

    assert sorted([r.status_code for r in responses]) == [200, 409]

    async with maker() as check:
        wallet = await check.get(Wallet, wallet_id)
        assert wallet.approval_state == "approved"
        logs = (
            (await check.execute(select(DecisionLogEntry))).scalars().all()
        )
        assert len([log for log in logs if log.action == "wallet_approved"]) == 1
    await s1.close()
    await s2.close()
    await engine.dispose()


async def test_decision_log_action_names_are_explicit(client, session):
    """The reject log entry is 'wallet_rejected', never 'wallet_rejectd'."""
    wallet = await _queue_wallet(session)
    resp = client.post(f"/wallets/{wallet.id}/reject")
    assert resp.status_code == 200
    logs = (await session.execute(select(DecisionLogEntry))).scalars().all()
    assert any(log.action == "wallet_rejected" for log in logs)
    assert not any(log.action == "wallet_rejectd" for log in logs)


async def test_scoring_run_endpoint_is_the_runtime_owner(client, session):
    """POST /scoring/run triggers score_all_wallets (Chunk 2 runtime owner).
    With no discovered/pending wallets it is a clean no-op."""
    resp = client.post("/scoring/run")
    assert resp.status_code == 200
    assert resp.json() == {"scored": 0, "verdicts": {}}
