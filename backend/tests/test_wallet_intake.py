"""Wallet intake: POST /wallets — the manual front door (PR-G).

Chunk 2 has no automated discovery; this endpoint is the ONLY way a
wallet enters the system. Rules under test:
  * valid address + optional label → wallet in ``discovered`` state,
    audited in the decision log;
  * address normalization (case-insensitive dedupe);
  * malformed addresses rejected (pydantic pattern → 422);
  * duplicates rejected with 409 carrying the current state;
  * nothing about intake can approve a wallet (no auto-approval field).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.db import get_db
from polycopy.main import app
from polycopy.models import Base, DecisionLogEntry, Wallet

VALID = "0x1234567890abcdef1234567890abcdef12345678"


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


async def test_add_wallet_lands_in_discovered_with_audit(client, session):
    resp = client.post(
        "/wallets", json={"address": VALID, "label": "Known sharp wallet"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["address"] == VALID
    assert body["label"] == "Known sharp wallet"
    assert body["approval_state"] == "discovered"

    wallet = (await session.execute(select(Wallet))).scalar_one()
    assert wallet.approval_state == "discovered"
    assert wallet.approved_at is None  # never copyable without human review

    logs = (await session.execute(select(DecisionLogEntry))).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "wallet_added"
    assert logs[0].actor == "human:api"
    assert logs[0].context["wallet"] == VALID


async def test_address_is_normalized_to_lowercase(client, session):
    mixed = "0xABCDEF1234567890ABCDEF1234567890ABCDEF12"
    resp = client.post("/wallets", json={"address": mixed})
    assert resp.status_code == 200
    assert resp.json()["address"] == mixed.lower()


async def test_duplicate_address_rejected_case_insensitively(client, session):
    assert client.post("/wallets", json={"address": VALID}).status_code == 200
    resp = client.post(
        "/wallets", json={"address": VALID.upper().replace("0X", "0x")}
    )
    assert resp.status_code == 409
    assert "already tracked" in resp.json()["detail"]
    # Only one row exists.
    assert len((await session.execute(select(Wallet))).scalars().all()) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "1234567890abcdef1234567890abcdef12345678",  # no 0x
        "0x123",  # too short
        "0xzzzz567890abcdef1234567890abcdef12345678",  # non-hex
        "0x1234567890abcdef1234567890abcdef1234567",  # 39 chars
        "0x1234567890abcdef1234567890abcdef123456789",  # 41 chars
    ],
)
async def test_malformed_addresses_rejected(client, bad):
    resp = client.post("/wallets", json={"address": bad})
    assert resp.status_code == 422


async def test_label_length_capped(client):
    resp = client.post("/wallets", json={"address": VALID, "label": "x" * 121})
    assert resp.status_code == 422


async def test_mixed_case_existing_row_is_detected(client, session):
    """REVIEW REGRESSION: rows seeded before lowercase normalization hold
    mixed-case addresses; the DB unique constraint is case-sensitive, so
    detection must be case-insensitive."""
    session.add(
        Wallet(address=VALID.upper().replace("0X", "0x"), approval_state="discovered")
    )
    await session.commit()
    resp = client.post("/wallets", json={"address": VALID})
    assert resp.status_code == 409
    assert "already tracked" in resp.json()["detail"]
    assert (
        len((await session.execute(select(Wallet))).scalars().all()) == 1
    )


async def test_concurrent_duplicate_insert_returns_clean_409(
    client, session, monkeypatch
):
    """REVIEW REGRESSION: two requests pass the existence check at the
    same time; the loser's flush hits the unique constraint. That must
    roll back and surface as the same clean 409 — never a 500.

    Simulated by forcing the pre-insert check to miss while the row
    already exists, and forcing flush to raise IntegrityError.
    """
    session.add(Wallet(address=VALID, approval_state="discovered"))
    await session.commit()

    real_execute = session.execute

    class _MissedCheck:
        def scalar_one_or_none(self):
            return None

    calls = {"seen": 0}

    async def racing_execute(stmt, *args, **kwargs):
        # Only the FIRST lower(address) lookup misses (the race window);
        # the post-rollback re-check runs for real and finds the row.
        if calls["seen"] == 0 and "lower" in str(stmt).lower():
            calls["seen"] += 1
            return _MissedCheck()
        return await real_execute(stmt, *args, **kwargs)

    async def losing_flush():
        raise IntegrityError("INSERT INTO wallets", {}, Exception("unique"))

    monkeypatch.setattr(session, "execute", racing_execute)
    monkeypatch.setattr(session, "flush", losing_flush)

    resp = client.post("/wallets", json={"address": VALID})
    assert resp.status_code == 409
    assert "already tracked" in resp.json()["detail"]
    assert (
        len((await session.execute(select(Wallet))).scalars().all()) == 1
    )
