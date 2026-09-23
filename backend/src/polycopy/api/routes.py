"""Scoring + approval queue endpoints.

The approval state machine has ONE source of truth:
``wallets.approval_state``. These endpoints are the ONLY human-driven
transitions (pending_review → approved | rejected; approved → disabled).
Every transition closes its queue entry and writes a DecisionLogEntry
with actor ``human:api``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.db import get_db
from polycopy.models import (
    ApprovalQueueEntry,
    DecisionLogEntry,
    Wallet,
    WalletScore,
)
from polycopy.scoring.service import score_all_wallets

router = APIRouter()

_WALLET_ADDRESS_PATTERN = r"^0x[0-9a-fA-F]{40}$"


class WalletCreateIn(BaseModel):
    """Manual candidate intake — Chunk 2 has no automated discovery."""

    address: str = Field(pattern=_WALLET_ADDRESS_PATTERN)
    label: str | None = Field(default=None, max_length=120)

_ALLOWED_TRANSITIONS = {
    "approve": ("pending_review", "approved"),
    "reject": ("pending_review", "rejected"),
    "disable": ("approved", "disabled"),
}

# Explicit decision-log action names — never derive verbs by string
# inflection ("reject" + "d" is how you get "wallet_rejectd").
_ACTION_LOG_NAMES = {
    "approve": "wallet_approved",
    "reject": "wallet_rejected",
    "disable": "wallet_disabled",
}


def _score_payload(score: WalletScore) -> dict:
    breakdown = score.behavioral_tags[0] if score.behavioral_tags else {}
    return {
        "wallet_id": score.wallet_id,
        "window_days": score.window_days,
        "composite_score": score.composite_score,
        "profit_factor": score.profit_factor,
        "breakdown": breakdown,
        "computed_at": score.computed_at.isoformat() if score.computed_at else None,
    }


async def _latest_score(db: AsyncSession, wallet_id: int) -> WalletScore | None:
    return (
        await db.execute(
            select(WalletScore)
            .where(WalletScore.wallet_id == wallet_id)
            .order_by(WalletScore.computed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_scores(
    db: AsyncSession, wallet_ids: list[int]
) -> dict[int, WalletScore]:
    """Latest score per wallet in one bounded query."""
    if not wallet_ids:
        return {}
    ranked = (
        select(
            WalletScore.id.label("score_id"),
            func.row_number()
            .over(
                partition_by=WalletScore.wallet_id,
                order_by=(WalletScore.computed_at.desc(), WalletScore.id.desc()),
            )
            .label("rn"),
        )
        .where(WalletScore.wallet_id.in_(wallet_ids))
        .subquery()
    )
    scores = (
        await db.execute(
            select(WalletScore)
            .join(ranked, WalletScore.id == ranked.c.score_id)
            .where(ranked.c.rn == 1)
        )
    ).scalars().all()
    return {score.wallet_id: score for score in scores}


@router.post("/wallets")
async def add_wallet(body: WalletCreateIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Manual candidate intake: register a wallet address to track.

    Discovery stays human-driven in Chunk 2 — this endpoint is the ONLY
    way a wallet enters the system. New wallets land in ``discovered``:
    ingestion starts tailing them for scoring, but nothing becomes
    copyable until a human approves them through the review queue.
    Addresses are normalized to lowercase; duplicates are rejected.
    """
    address = body.address.lower()
    existing = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"wallet {address} is already tracked "
            f"(state: {existing.approval_state})",
        )

    wallet = Wallet(address=address, label=body.label, approval_state="discovered")
    db.add(wallet)
    await db.flush()
    db.add(
        DecisionLogEntry(
            actor="human:api",
            action="wallet_added",
            context={"wallet": address, "label": body.label},
        )
    )
    await db.commit()
    return {
        "id": wallet.id,
        "address": wallet.address,
        "label": wallet.label,
        "approval_state": wallet.approval_state,
    }


@router.get("/wallets/{wallet_id}/score")
async def get_wallet_score(
    wallet_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    """Latest score for one wallet, with the full component breakdown."""
    score = await _latest_score(db, wallet_id)
    if score is None:
        raise HTTPException(status_code=404, detail="no score for this wallet")
    return _score_payload(score)


@router.post("/scoring/run")
async def run_scoring(db: AsyncSession = Depends(get_db)) -> dict:
    """Operator-triggered scoring pass over discovered/pending wallets.

    This endpoint is the runtime owner of ``score_all_wallets`` in
    Chunk 2: candidate discovery is manual, so scoring runs on demand
    (operator or cron hitting this route), not in the trading bot loop.
    """
    verdicts = await score_all_wallets(db)
    await db.commit()
    return {"scored": len(verdicts), "verdicts": verdicts}


@router.get("/approval-queue")
async def list_approval_queue(db: AsyncSession = Depends(get_db)) -> dict:
    """Pending approvals: wallets the scorer flagged ≥70 awaiting a human."""
    rows = (
        await db.execute(
            select(ApprovalQueueEntry, Wallet)
            .join(Wallet, ApprovalQueueEntry.wallet_id == Wallet.id)
            .where(ApprovalQueueEntry.state == "pending")
            .order_by(ApprovalQueueEntry.created_at)
            .limit(200)
        )
    ).all()
    scores = await _latest_scores(db, [wallet.id for _, wallet in rows])
    items = []
    for entry, wallet in rows:
        score = scores.get(wallet.id)
        items.append(
            {
                "entry_id": entry.id,
                "wallet_id": wallet.id,
                "address": wallet.address,
                "label": wallet.label,
                "queued_at": entry.created_at.isoformat() if entry.created_at else None,
                "score": _score_payload(score) if score else None,
            }
        )
    return {"items": items, "count": len(items)}


@router.post("/wallets/{wallet_id}/{action}")
async def transition_wallet(
    wallet_id: int, action: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """Human decision: approve / reject / disable a wallet.

    The state transition is a single conditional UPDATE: the required
    source state is part of the WHERE clause, so two concurrent requests
    cannot both succeed — exactly one row matches, the loser gets 409.
    """
    if action not in _ALLOWED_TRANSITIONS:
        raise HTTPException(status_code=404, detail="unknown action")
    required_from, target = _ALLOWED_TRANSITIONS[action]

    result = await db.execute(
        update(Wallet)
        .where(Wallet.id == wallet_id, Wallet.approval_state == required_from)
        .values(
            approval_state=target,
            # approved_at is the copy-enabled boundary: only trades ingested
            # after this moment may ever become signals (review fix, PR #7).
            **({"approved_at": datetime.now(UTC)} if target == "approved" else {}),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        # Either no such wallet, or it isn't in the required source state
        # (possibly because a concurrent request just moved it).
        current = (
            await db.execute(select(Wallet).where(Wallet.id == wallet_id))
        ).scalar_one_or_none()
        if current is None:
            raise HTTPException(status_code=404, detail="wallet not found")
        await db.refresh(current)  # report the true current state, not a stale copy
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot {action} from state '{current.approval_state}' "
                f"(requires '{required_from}')"
            ),
        )

    wallet = (
        await db.execute(select(Wallet).where(Wallet.id == wallet_id))
    ).scalar_one()
    await db.refresh(wallet)  # bypass any identity-map-stale state
    entry = (
        await db.execute(
            select(ApprovalQueueEntry).where(
                ApprovalQueueEntry.wallet_id == wallet.id,
                ApprovalQueueEntry.state == "pending",
            )
        )
    ).scalar_one_or_none()
    if entry is not None:
        entry.state = target
        entry.decided_at = datetime.now(UTC)
        entry.reviewer = "human:api"
    db.add(
        DecisionLogEntry(
            actor="human:api",
            action=_ACTION_LOG_NAMES[action],
            context={"wallet": wallet.address, "from": required_from, "to": target},
        )
    )
    await db.commit()
    return {"wallet_id": wallet.id, "approval_state": wallet.approval_state}
