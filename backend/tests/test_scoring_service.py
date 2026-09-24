"""Scoring service: stats assembly, persistence, verdict routing, lifecycle."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from polycopy.scoring import score as scoring_math
from polycopy.scoring.service import build_wallet_stats, score_all_wallets, score_wallet


class _EmptyBootstrapClient:
    async def get_trades(self, *_args, **_kwargs):
        return []

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
    fee=0,
    n_trades=2,
):
    """One market + trades. winner=None → open market.

    2× BUY 10 @ 0.50, "Up" wins → +10 realized per market (−10 + 20).
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
                fee=fee,
                traded_at=decided_at + timedelta(hours=j),
            )
        )


async def _seed_strong_wallet(session, address="0xstrong", settled_count=16, fee=0):
    """Eligible: settled markets decided 20–35d ago (age gate OK, most
    inside the 30d recency window), active yesterday, diversified wins."""
    wallet = Wallet(address=address, approval_state="discovered")
    session.add(wallet)
    await session.flush()
    for i in range(settled_count):
        await _add_market_with_trades(
            session, wallet, f"m{i}{address}",
            decided_at=NOW - timedelta(days=20 + i), fee=fee,
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
    assert result.composite is None
    persisted = (
        await session.execute(
            select(WalletScore)
            .where(WalletScore.wallet_id == wallet.id)
            .order_by(WalletScore.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert persisted.composite_score is None
    assert persisted.behavioral_tags[0]["verdict"] == "insufficient_history"
    await session.refresh(wallet)
    assert wallet.approval_state == "discovered"  # NOT rejected

    # The rescan pool must include it.
    results = await score_all_wallets(session, _EmptyBootstrapClient(), now=NOW)
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
    results = await score_all_wallets(session, _EmptyBootstrapClient(), now=NOW)
    assert results == {"0xstrong": "pending_review", "0xstrong2": "pending_review"}
    await session.refresh(approved)
    assert approved.approval_state == "approved"


async def test_positive_before_fees_negative_after_fees_fails_gate():
    """REVIEW REGRESSION: scoring must see FEE-ADJUSTED P&L.

    16 settled markets, each: 2× BUY 10 @ 0.50, "Up" wins.
    Before fees: +10 per market → +160 total (would pass the P&L gate).
    With $6 fee per fill: +160 − (32 fills × $6) = −32 → gate must fail.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        wallet = await _seed_strong_wallet(session, fee=6)

        from polycopy.accounting.service import compute_wallet_accounting
        acct = await compute_wallet_accounting(session, wallet)
        assert acct.summary["realized_pnl"] == Decimal(-32)  # +160 − 192

        result = await score_wallet(session, wallet, now=NOW)
        assert result.verdict == "score_rejected"
        assert any("pnl" in f.lower() for f in result.gate_failures)
    await engine.dispose()


async def test_pending_review_downgraded_to_discovered_withdraws_queue():
    """REVIEW REGRESSION: 76 → pending_review → 62 → removed from queue.

    Rescoring 10 days later: every settled decision falls out of the 30d
    recency window (recency ≈ 0), the composite drops below 70 — but the
    wallet is still active (11 days), so the verdict is `discovered`.
    The stale queue entry must be withdrawn, not left approvable.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        wallet = await _seed_strong_wallet(session)

        first = await score_wallet(session, wallet, now=NOW)
        assert first.verdict == "pending_review"
        assert wallet.approval_state == "pending_review"
        assert first.composite >= 70

        later = await score_wallet(session, wallet, now=NOW + timedelta(days=10))
        assert later.eligible is True
        assert later.composite < 70
        assert later.verdict == "discovered"

        await session.refresh(wallet)
        assert wallet.approval_state == "discovered"  # back in rescan pool
        entries = (
            (await session.execute(select(ApprovalQueueEntry))).scalars().all()
        )
        assert len(entries) == 1
        assert entries[0].state == "withdrawn"
        assert entries[0].decided_at is not None

        # The queue endpoint's contract: nothing pending remains.
        pending = (
            await session.execute(
                select(ApprovalQueueEntry).where(
                    ApprovalQueueEntry.state == "pending"
                )
            )
        ).scalars().all()
        assert pending == []
    await engine.dispose()


async def test_pending_review_to_score_rejected_withdraws_queue():
    """REVIEW REGRESSION: 76 → pending_review → score_rejected → withdrawn.

    Rescoring 20 days later: last trade is 21 days old → dormant evidence
    gate → score_rejected. The stale queue entry must still be withdrawn.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        wallet = await _seed_strong_wallet(session)

        first = await score_wallet(session, wallet, now=NOW)
        assert first.verdict == "pending_review"

        later = await score_wallet(session, wallet, now=NOW + timedelta(days=20))
        assert later.verdict == "score_rejected"
        assert any("inactive" in f for f in later.gate_failures)

        await session.refresh(wallet)
        assert wallet.approval_state == "discovered"  # machine never writes "rejected"
        entry = (await session.execute(select(ApprovalQueueEntry))).scalar_one()
        assert entry.state == "withdrawn"
    await engine.dispose()



async def test_90d_review_profitability_does_not_change_lifetime_score_methodology(session):
    wallet = await _seed_strong_wallet(session)

    # One recent loss belongs in both lifetime and 90-day accounting.
    await _add_market_with_trades(
        session,
        wallet,
        "recent-loss",
        winner="Down",
        decided_at=NOW - timedelta(days=10),
    )
    # One old loss changes lifetime PF only; it is outside the 90-day
    # decision-time review window.
    await _add_market_with_trades(
        session,
        wallet,
        "old-loss",
        winner="Down",
        decided_at=NOW - timedelta(days=120),
    )
    await session.commit()

    lifetime_stats = await build_wallet_stats(session, wallet, now=NOW)
    expected = scoring_math.score_wallet(lifetime_stats, now=NOW)
    result = await score_wallet(session, wallet, now=NOW)

    score = (
        await session.execute(
            select(WalletScore)
            .where(WalletScore.wallet_id == wallet.id)
            .order_by(WalletScore.id.desc())
            .limit(1)
        )
    ).scalar_one()

    # Existing lifetime metric and score inputs are untouched:
    # 16 × +10 winners / two × 10 losses = PF 8.
    assert score.profit_factor == pytest.approx(8.0)
    assert result.components == expected.components
    assert result.composite == expected.composite
    assert score.composite_score == expected.composite

    # Review-only 90-day metric excludes the old loss:
    # 16 × +10 winners / one × 10 loss = PF 16.
    assert score.profit_factor_90d == pytest.approx(16.0)
    assert score.gross_profit_90d == Decimal("160.000000")
    assert score.gross_loss_90d == Decimal("10.000000")
    assert score.realized_pnl_90d == Decimal("150.000000")


async def test_90d_profit_factor_preserves_existing_zero_loss_semantics(session):
    wallet = Wallet(address="0xzeroloss", approval_state="discovered")
    session.add(wallet)
    await session.flush()

    # Positive 90-day profit, zero gross loss: existing persisted PF semantics
    # represent the mathematical infinity as NULL rather than serializing inf.
    await _add_market_with_trades(
        session,
        wallet,
        "recent-win",
        decided_at=NOW - timedelta(days=1),
        n_trades=1,
    )
    # Add enough old history for scoring to run without changing the recent
    # zero-loss condition.
    for i in range(15):
        await _add_market_with_trades(
            session,
            wallet,
            f"old-win-{i}",
            decided_at=NOW - timedelta(days=100 + i),
        )
    await session.commit()

    await score_wallet(session, wallet, now=NOW)
    score = (
        await session.execute(
            select(WalletScore)
            .where(WalletScore.wallet_id == wallet.id)
            .order_by(WalletScore.id.desc())
            .limit(1)
        )
    ).scalar_one()

    assert score.gross_profit_90d > 0
    assert score.gross_loss_90d == Decimal("0.000000")
    assert score.profit_factor_90d is None


async def test_90d_no_settled_markets_persists_zero_totals_and_null_pf(session):
    wallet = Wallet(address="0xnorecentsettled", approval_state="discovered")
    session.add(wallet)
    await session.flush()

    # Historical resolved evidence only.
    for i in range(16):
        await _add_market_with_trades(
            session,
            wallet,
            f"historic-{i}",
            decided_at=NOW - timedelta(days=120 + i),
        )
    # Recent activity is open/unresolved, so 90-day realized totals remain zero.
    await _add_market_with_trades(
        session,
        wallet,
        "recent-open",
        winner=None,
        decided_at=NOW - timedelta(days=1),
        n_trades=1,
    )
    await session.commit()

    await score_wallet(session, wallet, now=NOW)
    score = (
        await session.execute(
            select(WalletScore)
            .where(WalletScore.wallet_id == wallet.id)
            .order_by(WalletScore.id.desc())
            .limit(1)
        )
    ).scalar_one()

    assert score.gross_profit_90d == Decimal("0.000000")
    assert score.gross_loss_90d == Decimal("0.000000")
    assert score.realized_pnl_90d == Decimal("0.000000")
    assert score.profit_factor_90d is None
