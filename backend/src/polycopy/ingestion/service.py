"""Bounded trade ingestion service.

Flow per wallet per cycle:

1. Fetch the most recent batch of trades (size capped at
   ``ingestion_batch_size`` — never unbounded pagination).
2. Compute the canonical key for each raw trade; skip any already in the
   DB (dedup is by the contract key, enforced by a unique constraint as
   the final backstop).
3. Get-or-create the referenced markets (Gamma metadata, best-effort) and
   insert new trades in one transaction.

The service is deliberately *not* a daemon loop here — the tailing bot
(PR-E) owns scheduling. This module exposes ``ingest_wallet_trades`` and
``run_ingestion_cycle`` so every piece is unit-testable with a mocked
client and an in-memory session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.config import get_settings
from polycopy.ingestion.client import (
    PolymarketAPIError,
    PolymarketClient,
    clob_token_map,
)
from polycopy.ingestion.identity import canonical_trade_id
from polycopy.logging_config import get_logger
from polycopy.models import Market, Trade, Wallet

logger = get_logger("polycopy.ingestion.service")


def _parse_traded_at(raw: dict[str, Any]) -> datetime:
    ts = raw.get("timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=UTC)
    # Tolerate ISO strings if the API ever changes shape.
    dt = datetime.fromisoformat(str(ts))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _get_or_create_market(
    session: AsyncSession,
    client: PolymarketClient,
    condition_id: str,
    *,
    fetch_metadata: bool,
) -> Market:
    market = (
        await session.execute(select(Market).where(Market.condition_id == condition_id))
    ).scalar_one_or_none()
    if market is not None:
        return market

    market = Market(condition_id=condition_id, question="")
    if fetch_metadata:
        try:
            gamma = await client.get_gamma_market(condition_id)
        except (PolymarketAPIError, ValueError) as exc:
            # Metadata is best-effort; an outage must not block trades.
            logger.warning(
                "gamma_metadata_failed", condition_id=condition_id, error=str(exc)
            )
        else:
            if gamma:
                market.question = str(gamma.get("question") or "")
                market.slug = gamma.get("slug")
                market.outcomes = gamma.get("outcomes")
                market.clob_token_ids = clob_token_map(gamma) or None
                market.active = bool(gamma.get("active", True))
                market.closed = bool(gamma.get("closed", False))
    session.add(market)
    await session.flush()
    return market


async def ingest_wallet_trades(
    session: AsyncSession,
    client: PolymarketClient,
    wallet: Wallet,
    *,
    fetch_market_metadata: bool = True,
) -> int:
    """Ingest one bounded batch of trades for one wallet.

    Returns the number of NEW trades inserted. Bounded by
    ``POLYCOPY_INGESTION_BATCH_SIZE`` — one API page, no unbounded loops.
    """
    settings = get_settings()
    raw_trades = await client.get_trades(
        wallet.address, limit=settings.ingestion_batch_size
    )
    if not raw_trades:
        return 0

    # Dedup within the batch AND against the DB in one lookup.
    keyed: dict[str, dict[str, Any]] = {}
    for raw in raw_trades:
        try:
            key = canonical_trade_id(raw)
        except ValueError as exc:
            logger.warning("trade_missing_identity", error=str(exc), wallet=wallet.address)
            continue
        keyed.setdefault(key, raw)

    if not keyed:
        return 0

    existing = (
        await session.execute(
            select(Trade.polymarket_trade_id).where(
                Trade.polymarket_trade_id.in_(list(keyed))
            )
        )
    ).scalars().all()
    new_keys = [k for k in keyed if k not in set(existing)]

    inserted = 0
    for key in new_keys:
        raw = keyed[key]
        market = await _get_or_create_market(
            session,
            client,
            str(raw["conditionId"]),
            fetch_metadata=fetch_market_metadata,
        )
        session.add(
            Trade(
                polymarket_trade_id=key,
                market_id=market.id,
                wallet_id=wallet.id,
                asset_id=str(raw.get("asset") or ""),
                side=str(raw["side"]).upper(),
                outcome=str(raw.get("outcome") or ""),
                size=raw["size"],
                price=raw["price"],
                traded_at=_parse_traded_at(raw),
            )
        )
        inserted += 1

    await session.commit()
    logger.info(
        "ingest_wallet_complete",
        wallet=wallet.address,
        fetched=len(raw_trades),
        new=inserted,
        skipped_duplicates=len(keyed) - inserted,
    )
    return inserted


async def run_ingestion_cycle(
    session: AsyncSession,
    client: PolymarketClient,
    *,
    include_unapproved: bool = True,
) -> dict[str, int]:
    """One bounded ingestion pass over tracked wallets.

    Wallets are processed SEQUENTIALLY — concurrency is capped inside the
    client, and serial wallet processing keeps DB pressure flat (the VPS
    OOM lesson). Returns {wallet_address: new_trade_count}.
    """
    stmt = select(Wallet)
    if not include_unapproved:
        stmt = stmt.where(Wallet.approval_state == "approved")
    wallets = (await session.execute(stmt)).scalars().all()

    results: dict[str, int] = {}
    for wallet in wallets:
        try:
            results[wallet.address] = await ingest_wallet_trades(session, client, wallet)
        except Exception:
            logger.exception("ingest_wallet_failed", wallet=wallet.address)
            await session.rollback()
            results[wallet.address] = 0
    return results
