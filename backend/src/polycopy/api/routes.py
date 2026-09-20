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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.db import get_db
from polycopy.models import (
    ApprovalQueueEntry,
    DecisionLogEntry,
    Wallet,
    WalletScore,
)

router = APIRouter()

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


@router.get("/wallets/{wallet_id}/score")
async def get_wallet_score(
    wallet_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    """Latest score for one wallet, with the full component breakdown."""
    score = await _latest_score(db, wallet_id)
    if score is None:
        raise HTTPException(status_code=404, detail="no score for this wallet")
    return _score_payload(score)


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
    items = []
    for entry, wallet in rows:
        score = await _latest_score(db, wallet.id)
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
