"""Persistent, bounded continuity checks for approved wallet trade transport."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.config import get_settings
from polycopy.ingestion.identity import canonical_trade_id
from polycopy.logging_config import get_logger
from polycopy.models import DecisionLogEntry, Trade, Wallet

logger = get_logger("polycopy.ingestion.catchup")


def _keys(rows: list[dict[str, Any]]) -> set[str]:
    keys = set()
    for row in rows:
        try:
            keys.add(canonical_trade_id(row))
        except (ValueError, ArithmeticError):
            continue
    return keys


async def ingest_approved_wallet(session: AsyncSession, client: Any, wallet: Wallet):
    """Fetch latest once, then bounded older pages until the prior anchor is seen.

    A page is checkpointed only after its trades commit. On interruption the
    page is replayed, with the canonical trade key providing deduplication.
    ``end`` freezes the recovery window while newer pages are still polled on
    subsequent cycles. An incomplete window is never declared caught up just
    because a later latest page overlaps trades inserted by this recovery.
    """
    from polycopy.ingestion.service import WalletIngestionResult, ingest_trade_rows

    settings = get_settings()
    size = min(max(1, settings.ingestion_batch_size), 500)
    budget = max(1, getattr(settings, "catch_up_pages_per_cycle", 3))
    prior = (await session.execute(
        select(DecisionLogEntry)
        .where(DecisionLogEntry.action == "wallet_catch_up",
               DecisionLogEntry.context["wallet_id"].as_integer() == wallet.id)
        .order_by(DecisionLogEntry.id.desc()).limit(1)
    )).scalar_one_or_none()
    state = dict(prior.context) if prior else {}
    # Existing approved wallets need an anchor on first deployment, using
    # their already persisted source history rather than assuming a baseline.
    anchor = state.get("anchor_id")
    if not anchor:
        anchor = await session.scalar(
            select(Trade.polymarket_trade_id)
            .where(Trade.wallet_id == wallet.id)
            .order_by(Trade.traded_at.desc(), Trade.id.desc()).limit(1)
        )

    latest = await client.get_trades(wallet.address, limit=size)
    latest_keys = _keys(latest)
    latest_anchor = next((canonical_trade_id(row) for row in latest
                          if _keys([row])), anchor)
    result = await ingest_trade_rows(session, client, wallet, latest,
                                      log_event="approved_wallet_tail_ingested")
    inserted, quarantined = result.inserted, result.quarantined
    incomplete = bool(state.get("catch_up_incomplete"))
    head_anchor = state.get("head_anchor", anchor)
    if not incomplete or head_anchor in latest_keys:
        head_anchor = latest_anchor
    # An existing incomplete window always keeps its original anchor and
    # cursor, even if the new latest page now overlaps inserted recovery rows.
    if not incomplete and anchor and anchor not in latest_keys:
        incomplete = True
        state = {
            "anchor_id": anchor,
            "head_anchor": latest_anchor,
            "end_timestamp": max((int(row.get("timestamp", 0)) for row in latest),
                                 default=int(datetime.now(UTC).timestamp())),
            "next_offset": 0,
            "window_oldest": None,
            "catch_up_incomplete": True,
        }
    elif incomplete and anchor in latest_keys:
        incomplete = False

    async def checkpoint(reason: str) -> None:
        session.add(DecisionLogEntry(actor="bot", action="wallet_catch_up", context={
            "wallet": wallet.address, "wallet_id": wallet.id,
            "anchor_id": state.get("anchor_id") if incomplete else latest_anchor,
            "head_anchor": head_anchor,
            "end_timestamp": state.get("end_timestamp") if incomplete else None,
            "next_offset": state.get("next_offset", 0) if incomplete else 0,
            "window_oldest": state.get("window_oldest") if incomplete else None,
            "catch_up_incomplete": incomplete,
            "termination_reason": reason,
        }))
        await session.commit()

    if not incomplete:
        if not prior or latest_anchor != anchor:
            await checkpoint("overlap_found" if anchor else "initial_baseline")
        return WalletIngestionResult(inserted=inserted, quarantined=quarantined)

    # Preserve the window and anchor before any further network call.
    await checkpoint("recovery_in_progress")
    for _ in range(budget):
        # Approval can change between cycles or during an awaited request.
        await session.refresh(wallet)
        if wallet.approval_state != "approved":
            await checkpoint("wallet_disabled")
            break
        offset = int(state["next_offset"])
        if offset >= 10000:
            oldest = state.get("window_oldest")
            if oldest is None or int(oldest) >= int(state["end_timestamp"]):
                await checkpoint("window_stalled")
                break
            state["end_timestamp"] = int(oldest)  # inclusive boundary overlap
            state["next_offset"] = 0
            offset = 0
            await checkpoint("window_rolled")
        rows = await client.get_trades(wallet.address, limit=min(size, 10000 - offset),
                                       offset=offset, start=1, end=int(state["end_timestamp"]))
        keys = _keys(rows)
        page = await ingest_trade_rows(session, client, wallet, rows,
                                       log_event="approved_wallet_catch_up_page")
        inserted += page.inserted
        quarantined += page.quarantined
        state["next_offset"] = offset + len(rows)
        times = [int(row["timestamp"]) for row in rows
                 if isinstance(row.get("timestamp"), (int, float))]
        if times:
            state["window_oldest"] = min(times)
        if anchor in keys:
            # The older window is connected. If newer trades have already
            # moved beyond its first observed head, start the next bounded
            # window from that head rather than silently skipping the gap.
            if head_anchor and head_anchor not in latest_keys:
                state = {
                    "anchor_id": head_anchor,
                    "end_timestamp": max((int(row.get("timestamp", 0)) for row in latest),
                                         default=int(datetime.now(UTC).timestamp())),
                    "next_offset": 0,
                    "window_oldest": None,
                }
                head_anchor = latest_anchor
                await checkpoint("next_window_pending")
            else:
                incomplete = False
                await checkpoint("overlap_found")
            break
        reason = "history_exhausted" if len(rows) < min(size, 10000 - offset) else "budget_reached"
        await checkpoint(reason)
        if reason == "history_exhausted":
            break
    if incomplete and (not prior or prior.context.get("next_offset") != state["next_offset"]):
        logger.warning("approved_wallet_catch_up_incomplete", wallet=wallet.address,
                       next_offset=state["next_offset"], end_timestamp=state["end_timestamp"])
    return WalletIngestionResult(inserted=inserted, quarantined=quarantined)
