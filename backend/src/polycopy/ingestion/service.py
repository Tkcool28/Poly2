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

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.config import get_settings
from polycopy.failure_throttle import FailureLogThrottle
from polycopy.ingestion.client import (
    PolymarketAPIError,
    PolymarketClient,
    clob_token_map,
)
from polycopy.ingestion.identity import canonical_trade_id
from polycopy.logging_config import get_logger
from polycopy.models import DecisionLogEntry, Market, Trade, Wallet

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


@dataclass(frozen=True)
class WalletIngestionResult:
    inserted: int
    quarantined: int


@dataclass(frozen=True)
class IngestionCycleResult:
    wallets: dict[str, int]
    failed_wallets: int
    quarantined_rows: int


_row_failure_throttle = FailureLogThrottle(trace_interval_seconds=300.0, summary_every=20)
_wallet_failure_throttle = FailureLogThrottle(trace_interval_seconds=300.0, summary_every=20)


def _identity_context(raw: dict[str, Any]) -> dict[str, str | None]:
    keys = ("transactionHash", "proxyWallet", "asset", "conditionId", "timestamp")
    return {key: (str(raw[key]) if raw.get(key) is not None else None) for key in keys}


def _record_quarantine(
    session: AsyncSession,
    *,
    wallet: Wallet,
    reason: str,
    error: Exception,
    raw: dict[str, Any],
    canonical_id: str | None = None,
) -> None:
    signature = (
        wallet.address,
        reason,
        type(error).__name__,
        str(error),
        canonical_id or str(raw.get("transactionHash") or ""),
    )
    decision = _row_failure_throttle.record(signature)
    if not (decision.full_trace or decision.emit_summary):
        return

    fields = {
        "wallet": wallet.address,
        "reason": reason,
        "error_type": type(error).__name__,
        "error": str(error),
        "identity": _identity_context(raw),
        "canonical_trade_id": canonical_id,
        "repeat_count": decision.repeat_count,
    }
    if decision.full_trace:
        logger.exception("trade_ingest_quarantined", **fields)
    else:
        logger.warning("trade_ingest_quarantined_repeated", **fields)

    session.add(
        DecisionLogEntry(
            actor="bot",
            action="trade_ingest_quarantined",
            context=fields,
        )
    )


async def ingest_wallet_trades(
    session: AsyncSession,
    client: PolymarketClient,
    wallet: Wallet,
    *,
    fetch_market_metadata: bool = True,
) -> WalletIngestionResult:
    """Ingest one bounded batch of trades for one wallet.

    Each row is flushed inside a SAVEPOINT. A permanently bad row rolls back
    only itself; it cannot poison the outer wallet transaction or later rows.
    """
    settings = get_settings()
    raw_trades = await client.get_trades(
        wallet.address, limit=settings.ingestion_batch_size
    )
    if not raw_trades:
        return WalletIngestionResult(inserted=0, quarantined=0)

    keyed: dict[str, dict[str, Any]] = {}
    quarantined = 0
    for raw in raw_trades:
        try:
            key = canonical_trade_id(raw)
        except (ValueError, ArithmeticError) as exc:
            quarantined += 1
            _record_quarantine(
                session,
                wallet=wallet,
                reason="invalid_identity",
                error=exc,
                raw=raw,
            )
            continue
        keyed.setdefault(key, raw)

    if not keyed:
        await session.commit()
        return WalletIngestionResult(inserted=0, quarantined=quarantined)

    existing = (
        await session.execute(
            select(Trade.polymarket_trade_id).where(
                Trade.polymarket_trade_id.in_(list(keyed))
            )
        )
    ).scalars().all()
    existing_set = set(existing)
    new_keys = [key for key in keyed if key not in existing_set]

    inserted = 0
    for key in new_keys:
        raw = keyed[key]
        try:
            async with session.begin_nested():
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
                await session.flush()
        except Exception as exc:
            quarantined += 1
            _record_quarantine(
                session,
                wallet=wallet,
                reason="row_persistence_failed",
                error=exc,
                raw=raw,
                canonical_id=key,
            )
            continue
        inserted += 1

    await session.commit()
    logger.info(
        "ingest_wallet_complete",
        wallet=wallet.address,
        fetched=len(raw_trades),
        new=inserted,
        quarantined=quarantined,
        skipped_duplicates=len(keyed) - len(new_keys),
    )
    return WalletIngestionResult(inserted=inserted, quarantined=quarantined)


async def run_ingestion_cycle(
    session: AsyncSession,
    client: PolymarketClient,
    *,
    include_unapproved: bool = True,
) -> IngestionCycleResult:
    """One bounded ingestion pass over tracked wallets with failure isolation."""
    stmt = select(Wallet)
    if not include_unapproved:
        stmt = stmt.where(Wallet.approval_state == "approved")
    wallets = (await session.execute(stmt)).scalars().all()

    results: dict[str, int] = {}
    failed_wallets = 0
    quarantined_rows = 0
    for wallet in wallets:
        try:
            result = await ingest_wallet_trades(session, client, wallet)
            results[wallet.address] = result.inserted
            quarantined_rows += result.quarantined
        except Exception as exc:
            failed_wallets += 1
            await session.rollback()
            results[wallet.address] = 0
            signature = (wallet.address, type(exc).__name__, str(exc))
            decision = _wallet_failure_throttle.record(signature)
            if decision.full_trace:
                logger.exception(
                    "ingest_wallet_failed",
                    wallet=wallet.address,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    repeat_count=decision.repeat_count,
                )
            elif decision.emit_summary:
                logger.warning(
                    "ingest_wallet_failed_repeated",
                    wallet=wallet.address,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    repeat_count=decision.repeat_count,
                )

    return IngestionCycleResult(
        wallets=results,
        failed_wallets=failed_wallets,
        quarantined_rows=quarantined_rows,
    )
