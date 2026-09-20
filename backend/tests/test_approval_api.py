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
            composite_score=82.5,
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
    assert item["score"]["breakdown"]["components"]["pnl_quality"] == 80.0


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
