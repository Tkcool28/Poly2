"""Scoring service: stats assembly, persistence, verdict routing, lifecycle."""

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


async def _add_market_with_trades(
    session,
    wallet,
    key: str,
    *,
    winner: str | None = "Up",
    decided_at: datetime,
    settled_at: datetime | None = None,
    size=10,
    price=0.50,
    n_trades=2,
):
    """One market + trades. winner=None → open market.

    BUY size @ 0.50, "Up" wins → +size/2 realized per market (10 → +5).
    """
    market = Market(
        condition_id=key,
        question="?",
        closed=winner is not None,
        resolved_outcome=winner,
    )
    session.add(market)
    await session.flush()
    if winner is not None:
        session.add(
            Settlement(
                market_id=market.id,
                winning_outcome=winner,
                settled_at=settled_at or decided_at,
            )
        )
    for j in range(n_trades):
        session.add(
            Trade(
                polymarket_trade_id=f"data-api:0x{key}{j}:{wallet.address}:1:{size}:{price}:1",
                market_id=market.id,
                wallet_id=wallet.id,
                asset_id="1",
                side="BUY",
                outcome="Up",
                size=size,
                price=price,
                traded_at=decided_at + timedelta(hours=j),
            )
        )


async def _seed_strong_wallet(session, address="0xstrong", settled_count=16):
    """Eligible: settled markets decided 20–35d ago (age gate OK, most
    inside the 30d recency window), active yesterday, diversified wins."""
    wallet = Wallet(address=address, approval_state="discovered")
    session.add(wallet)
    await session.flush()
    for i in range(settled_count):
        await _add_market_with_trades(
            session, wallet, f"m{i}{address}",
            decided_at=NOW - timedelta(days=20 + i),
        )
    # one recent open-market trade → active now
    await _add_market_with_trades(
        session, wallet, f"open{address}", winner=None,
        decided_at=NOW - timedelta(days=1), size=5, n_trades=1,
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
        "sample_strength", "concentration", "profit_factor", "consistency",
        "recency",
    }
    assert breakdown["weights"]["sample_strength"] == 0.25

    log = (await session.execute(select(DecisionLogEntry))).scalar_one()
    assert log.actor == "scorer"
    assert log.context["verdict"] == "pending_review"


async def test_immature_wallet_stays_discovered_and_rescannable(session):
    """REVIEW REGRESSION: insufficient history is NOT a terminal rejection.

    14 settled markets today → insufficient_history → state stays
    discovered → wallet is picked up by future scoring cycles → after
    more history arrives it reaches pending_review.
    """
    wallet = await _seed_strong_wallet(session, settled_count=14)

    result = await score_wallet(session, wallet, now=NOW)
    assert result.verdict == "insufficient_history"
    await session.refresh(wallet)
    assert wallet.approval_state == "discovered"  # NOT rejected

    # The rescan pool must include it.
    results = await score_all_wallets(session, now=NOW)
    assert wallet.address in results

    # Two more settled markets arrive; next cycle re-evaluates.
    for i in range(2):
        await _add_market_with_trades(
            session, wallet, f"late{i}",
            decided_at=NOW - timedelta(days=2 + i),
        )
    await session.commit()

    result = await score_wallet(session, wallet, now=NOW)
    assert result.eligible is True
    assert result.verdict == "pending_review"
    await session.refresh(wallet)
    assert wallet.approval_state == "pending_review"


async def test_machine_rejection_does_not_touch_approval_state(session):
    """score_rejected is recorded, but approval_state=rejected is reserved
    for human decisions."""
    wallet = Wallet(address="0xdormant", approval_state="discovered")
    session.add(wallet)
    await session.flush()
    # Adequate history but dormant: 16 settled markets, old trades.
    for i in range(16):
        await _add_market_with_trades(
            session, wallet, f"d{i}", decided_at=NOW - timedelta(days=100 + i)
        )
    await session.commit()

    result = await score_wallet(session, wallet, now=NOW)
    assert result.verdict == "score_rejected"
    assert any("inactive" in f for f in result.gate_failures)
    await session.refresh(wallet)
    assert wallet.approval_state == "discovered"  # machine never writes "rejected"


async def test_human_decisions_never_overridden(session):
    for state in ("approved", "rejected", "disabled"):
        wallet = await _seed_strong_wallet(session, address=f"0x{state}")
        wallet.approval_state = state
        await session.commit()
        await score_wallet(session, wallet, now=NOW)
        await session.refresh(wallet)
        assert wallet.approval_state == state


async def test_recency_uses_decision_time_not_settlement_time(session):
    """REVIEW REGRESSION: old decisions settling recently are NOT recent.

    Wallet A decided everything ~200 days ago; markets settled yesterday.
    Wallet B made identical decisions 5–20 days ago; settled yesterday.
    A's recency must be 0, B's must be 100.
    """
    async def make_wallet(address, decided_days_ago):
        wallet = Wallet(address=address, approval_state="discovered")
        session.add(wallet)
        await session.flush()
        for i in range(16):
            await _add_market_with_trades(
                session, wallet, f"r{i}{address}",
                decided_at=NOW - timedelta(days=decided_days_ago + i),
                settled_at=NOW - timedelta(days=1),  # settled recently!
            )
        # age anchor (old open-market trade) + activity anchor (recent)
        await _add_market_with_trades(
            session, wallet, f"rage{address}", winner=None,
            decided_at=NOW - timedelta(days=max(decided_days_ago, 40)),
            size=5, n_trades=1,
        )
        await _add_market_with_trades(
            session, wallet, f"ract{address}", winner=None,
            decided_at=NOW - timedelta(days=1), size=5, n_trades=1,
        )
        await session.commit()
        return wallet

    old = await make_wallet("0xold", 200)
    new = await make_wallet("0xnew", 5)

    result_old = await score_wallet(session, old, now=NOW)
    result_new = await score_wallet(session, new, now=NOW)
    assert result_old.eligible and result_new.eligible
    assert result_old.components["recency"] == 0.0
    assert result_new.components["recency"] == 100.0


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
