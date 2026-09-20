"""Scoring service: stats assembly, persistence, verdict routing."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.models import (
    ApprovalQueueEntry,
    Base,
    DecisionLogEntry,
    Market,
    Settlement,
    Trade,
    Wallet,
    WalletScore,
)
from polycopy.scoring.service import score_all_wallets, score_wallet

NOW = datetime(2026, 9, 20, tzinfo=UTC)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed_strong_wallet(session, address="0xstrong"):
    """Eligible wallet: 16 settled markets, 32 trades, diversified wins,
    active yesterday, 90 days of history."""
    wallet = Wallet(address=address, approval_state="discovered")
    session.add(wallet)
    await session.flush()
    for i in range(16):
        market = Market(
            condition_id=f"0xm{i}{address}", question="?", closed=True, resolved_outcome="Up"
        )
        session.add(market)
        await session.flush()
        session.add(
            Settlement(
                market_id=market.id,
                winning_outcome="Up",
                settled_at=NOW - timedelta(days=10 + i),
            )
        )
        # Buy 10 Up @ 0.50 → −5, wins → +10 ⇒ +5 per market ⇒ +80 total
        for j in range(2):
            session.add(
                Trade(
                    polymarket_trade_id=f"data-api:0x{i}{j}:{address}:1:10:0.5:1",
                    market_id=market.id,
                    wallet_id=wallet.id,
                    asset_id="1",
                    side="BUY",
                    outcome="Up",
                    size=10,
                    price=0.50,
                    traded_at=NOW - timedelta(days=30 + i, hours=j),
                )
            )
    # one recent open-market trade → active now
    market = Market(condition_id=f"0xopen{address}", question="?")
    session.add(market)
    await session.flush()
    session.add(
        Trade(
            polymarket_trade_id=f"data-api:0xrecent:{address}:1:5:0.5:1",
            market_id=market.id,
            wallet_id=wallet.id,
            asset_id="1",
            side="BUY",
            outcome="Up",
            size=5,
            price=0.5,
            traded_at=NOW - timedelta(days=1),
        )
    )
    await session.commit()
    return wallet


async def test_strong_wallet_scored_and_queued(session):
    wallet = await _seed_strong_wallet(session)
    result = await score_wallet(session, wallet, now=NOW)
    assert result.eligible is True
    assert result.verdict == "pending_review"

    await session.refresh(wallet)
    assert wallet.approval_state == "pending_review"

    entry = (await session.execute(select(ApprovalQueueEntry))).scalar_one()
    assert entry.state == "pending"

    score = (await session.execute(select(WalletScore))).scalar_one()
    assert score.composite_score is not None and score.composite_score >= 70
    breakdown = score.behavioral_tags[0]
    assert breakdown["components"].keys() == {
        "pnl_quality", "concentration", "profit_factor", "consistency", "recency"
    }
    assert breakdown["weights"]["pnl_quality"] == 0.25

    log = (await session.execute(select(DecisionLogEntry))).scalar_one()
    assert log.actor == "scorer"
    assert log.context["verdict"] == "pending_review"


async def test_ineligible_wallet_rejected_with_reason(session):
    wallet = Wallet(address="0xtiny", approval_state="discovered")
    session.add(wallet)
    await session.commit()
    result = await score_wallet(session, wallet, now=NOW)
    assert result.eligible is False
    assert result.verdict == "rejected"
    await session.refresh(wallet)
    assert wallet.approval_state == "rejected"
    assert result.gate_failures  # reasons recorded


async def test_human_decisions_never_overridden(session):
    for state in ("approved", "rejected", "disabled"):
        wallet = await _seed_strong_wallet(session, address=f"0x{state}")
        wallet.approval_state = state
        await session.commit()
        await score_wallet(session, wallet, now=NOW)
        await session.refresh(wallet)
        assert wallet.approval_state == state


async def test_scoring_is_repeatable_without_duplicate_queue_entries(session):
    wallet = await _seed_strong_wallet(session)
    await score_wallet(session, wallet, now=NOW)
    await score_wallet(session, wallet, now=NOW)
    count = await session.scalar(
        select(func.count(ApprovalQueueEntry.id)).where(
            ApprovalQueueEntry.state == "pending"
        )
    )
    assert count == 1
    assert (await session.scalar(select(func.count(WalletScore.id)))) == 2


async def test_score_all_wallets_isolates_failures(session):
    await _seed_strong_wallet(session, "0xstrong")
    await _seed_strong_wallet(session, "0xstrong2")
    approved = Wallet(address="0xapproved", approval_state="approved")
    session.add(approved)
    await session.commit()
    results = await score_all_wallets(session, now=NOW)
    assert results == {"0xstrong": "pending_review", "0xstrong2": "pending_review"}
    await session.refresh(approved)
    assert approved.approval_state == "approved"
