"""Bounded trade ingestion service.

Flow per wallet per cycle:

1. Fetch the most recent batch of trades (size capped at
   ``ingestion_batch_size`` — never unbounded pagination).
2. Compute the canonical key for each raw trade; skip any already in the
   DB (dedup is by the contract key, enforced by a unique constraint as
   the final backstop).
3. Get-or-create the referenced markets (Gamma metadata, best-effort) and
   insert new trades in one transaction.

The candidate scoring endpoint may separately call
``bootstrap_wallet_history``. That path uses a persisted offset and explicit
trade/page/request bounds; it is never called from recurring live tailing.

The service is deliberately *not* a daemon loop here — the tailing bot
(PR-E) owns scheduling. This module exposes ``ingest_wallet_trades`` and
``run_ingestion_cycle`` so every piece is unit-testable with a mocked
client and an in-memory session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.accounting.settlements import gamma_backoff_active, refresh_settlements
from polycopy.config import get_settings
from polycopy.failure_throttle import FailureLogThrottle
from polycopy.ingestion.client import (
    PolymarketAPIError,
    PolymarketClient,
    clob_token_map,
)
from polycopy.ingestion.identity import canonical_trade_id
from polycopy.logging_config import get_logger
from polycopy.models import DecisionLogEntry, Market, Settlement, Trade, Wallet
from polycopy.scoring.score import MIN_ACCOUNT_AGE_DAYS, MIN_SETTLED_MARKETS, MIN_TRADES

logger = get_logger("polycopy.ingestion.service")


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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


async def bootstrap_wallet_history(
    session: AsyncSession,
    client: PolymarketClient,
    wallet: Wallet,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backfill a newly discovered wallet within explicit cumulative bounds.

    Data API pages are newest-first and use limit/offset with a fixed `end`
    timestamp. Offset state is committed only after page persistence, making
    an interrupted page safely replayable under canonical trade dedup.
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    prior = (
        await session.execute(
            select(DecisionLogEntry)
            .where(
                DecisionLogEntry.action == "wallet_historical_bootstrap",
                DecisionLogEntry.context["wallet_id"].as_integer() == wallet.id,
            )
            .order_by(DecisionLogEntry.id.desc())
            .limit(1)
        )
    ).scalars().all()
    state = dict(prior[0].context) if prior else {}
    if state.get("maturity_satisfied"):
        return state

    local_count = int(await session.scalar(
        select(func.count(Trade.id)).where(Trade.wallet_id == wallet.id)
    ) or 0)
    settled_count = int(await session.scalar(
        select(func.count(func.distinct(Settlement.market_id)))
        .join(Market, Market.id == Settlement.market_id)
        .join(Trade, Trade.market_id == Market.id)
        .where(Trade.wallet_id == wallet.id)
    ) or 0)
    bounds = (await session.execute(
        select(func.min(Trade.traded_at), func.max(Trade.traded_at))
        .where(Trade.wallet_id == wallet.id)
    )).one()
    initial_oldest = bounds[0]
    if (
        local_count >= MIN_TRADES
        and settled_count >= MIN_SETTLED_MARKETS
        and initial_oldest is not None
        and (now - (initial_oldest if initial_oldest.tzinfo else initial_oldest.replace(tzinfo=UTC))).days >= MIN_ACCOUNT_AGE_DAYS
    ):
        record = {
            "wallet": wallet.address, "wallet_id": wallet.id,
            "pages_requested": 0, "rows_fetched": 0,
            "new_trades_inserted": 0, "duplicates_skipped": 0,
            "quarantined_rows": 0,
            "oldest_trade_observed": initial_oldest.isoformat(),
            "newest_trade_observed": bounds[1].isoformat() if bounds[1] else None,
            "settled_market_count": settled_count,
            "total_local_trade_count": local_count,
            "maturity_satisfied": True, "termination_reason": "maturity_satisfied",
            "next_offset": 0, "end_timestamp": int(now.timestamp()),
            "requests_used": 0, "settlement_checked": [],
        }
        session.add(DecisionLogEntry(actor="bot", action="wallet_historical_bootstrap", context=record))
        await session.commit()
        return record
    if state.get("termination_reason") == "history_exhausted":
        return state

    max_trades = max(1, settings.bootstrap_max_trades)
    page_size = min(max(1, settings.bootstrap_page_size), max_trades, 500)
    max_pages = max(1, settings.bootstrap_max_pages)
    max_requests = max(1, settings.bootstrap_max_requests)
    if state.get("termination_reason") == "configured_limit_reached" and (
        int(state.get("pages_requested", 0)) >= max_pages
        or int(state.get("rows_fetched", 0)) >= max_trades
        or int(state.get("requests_used", 0)) >= max_requests
    ):
        return state
    end_ts = int(state.get("end_timestamp") or now.timestamp())
    offset = int(state.get("next_offset", 0))
    window_oldest = state.get("window_oldest")
    pages = int(state.get("pages_requested", 0))
    rows_fetched = int(state.get("rows_fetched", 0))
    requests = int(state.get("requests_used", 0))
    settlement_checked = list(state.get("settlement_checked", []))
    inserted_total = 0
    duplicates_total = 0
    quarantined_total = 0
    termination = "configured_limit_reached"
    mature = False
    oldest: datetime | None = None
    newest: datetime | None = None
    pages_this_run = 0

    async def evidence() -> tuple[int, int, datetime | None, datetime | None]:
        count = int(await session.scalar(
            select(func.count(Trade.id)).where(Trade.wallet_id == wallet.id)
        ) or 0)
        bounds = (await session.execute(
            select(func.min(Trade.traded_at), func.max(Trade.traded_at))
            .where(Trade.wallet_id == wallet.id)
        )).one()
        settled = int(await session.scalar(
            select(func.count(func.distinct(Settlement.market_id)))
            .join(Market, Market.id == Settlement.market_id)
            .join(Trade, Trade.market_id == Market.id)
            .where(Trade.wallet_id == wallet.id)
        ) or 0)
        return count, settled, bounds[0], bounds[1]

    async def persist_progress(reason: str = "in_progress") -> None:
        """Durably reserve request budget and checkpoint completed pages."""
        session.add(DecisionLogEntry(
            actor="bot",
            action="wallet_historical_bootstrap",
            context={
                "wallet": wallet.address,
                "wallet_id": wallet.id,
                "pages_requested": pages,
                "rows_fetched": rows_fetched,
                "new_trades_inserted": int(state.get("new_trades_inserted", 0)) + inserted_total,
                "duplicates_skipped": int(state.get("duplicates_skipped", 0)) + duplicates_total,
                "quarantined_rows": int(state.get("quarantined_rows", 0)) + quarantined_total,
                "oldest_trade_observed": oldest.isoformat() if oldest else None,
                "newest_trade_observed": newest.isoformat() if newest else None,
                "settled_market_count": int(state.get("settled_market_count", 0)),
                "total_local_trade_count": int(state.get("total_local_trade_count", 0)),
                "maturity_satisfied": False,
                "termination_reason": reason,
                "next_offset": offset,
                "end_timestamp": end_ts,
                "window_oldest": window_oldest,
                "requests_used": requests,
                "settlement_checked": settlement_checked,
            },
        ))
        await session.commit()

    while (pages < max_pages and rows_fetched < max_trades
           and requests < max_requests
           and pages_this_run < getattr(settings, "bootstrap_pages_per_run", max_pages)):
        if offset >= 10000:
            if window_oldest is None or int(window_oldest) >= end_ts:
                termination = "configured_limit_reached"
                break
            end_ts = int(window_oldest)  # inclusive: boundary trades can overlap
            offset = 0
            await persist_progress()
        # Reserve the request and page slot before hitting the network. A
        # process interruption cannot silently reset the hard budget.
        pages += 1
        pages_this_run += 1
        requests += 1
        await persist_progress()
        requested_limit = min(page_size, max_trades - rows_fetched, 10000 - offset)
        try:
            raw = await client.get_trades(
                wallet.address, limit=requested_limit,
                offset=offset, start=1, end=end_ts,
            )
        except (PolymarketAPIError, ValueError, OSError) as exc:
            termination = "upstream_error"
            logger.warning("wallet_bootstrap_upstream_error", wallet=wallet.address, error=str(exc))
            break
        rows_fetched += len(raw)
        if not raw:
            termination = "history_exhausted"
            break
        parsed_times: list[datetime] = []
        for item in raw:
            try:
                parsed_times.append(_parse_traded_at(item))
            except (ValueError, TypeError, OverflowError):
                continue
        if parsed_times:
            page_oldest, page_newest = min(parsed_times), max(parsed_times)
            window_oldest = int(page_oldest.timestamp())
            oldest = min(_aware_utc(oldest), page_oldest) if oldest else page_oldest
            newest = max(_aware_utc(newest), page_newest) if newest else page_newest

        page_result = await ingest_trade_rows(
            session, client, wallet, raw, fetch_market_metadata=False,
            log_event="wallet_bootstrap_page_ingested",
        )
        inserted_total += page_result.inserted
        quarantined_total += page_result.quarantined
        duplicates_total += max(0, len(raw) - page_result.inserted - page_result.quarantined)
        offset += len(raw)
        await persist_progress()

        # Reconcile oldest observed unsettled markets first. Each candidate
        # market consumes at most two logical API requests (exact Gamma then
        # identity-checked token fallback); the hard total request budget is
        # enforced before advancing again.
        remaining_settlement_markets = max(
            0, settings.bootstrap_max_settlement_markets - len(settlement_checked)
        )
        if await gamma_backoff_active(session, now):
            remaining_settlement_markets = 0
        # A satisfied settlement gate needs no further Gamma calls; older
        # trade pages may still be required solely to establish account age.
        current_settled = int(await session.scalar(
            select(func.count(func.distinct(Settlement.market_id)))
            .join(Trade, Trade.market_id == Settlement.market_id)
            .where(Trade.wallet_id == wallet.id)
        ) or 0)
        market_rows = (await session.execute(
            select(Market.condition_id)
            .join(Trade, Trade.market_id == Market.id)
            .where(Trade.wallet_id == wallet.id)
            .where(Market.id.not_in(select(Settlement.market_id)))
            .where(or_(Market.settlement_next_check_at.is_(None),
                       Market.settlement_next_check_at <= now))
            .where(Market.condition_id.not_in(settlement_checked))
            .group_by(Market.id, Market.condition_id)
            .order_by(func.min(Trade.traded_at))
            .limit(min(
                remaining_settlement_markets if current_settled < MIN_SETTLED_MARKETS else 0,
                max(0, (max_requests - requests) // 2),
            ))
        )).scalars().all()
        if market_rows:
            # Count worst-case exact lookup plus validated token fallback per
            # market before starting settlement discovery.
            requests += 2 * len(market_rows)
            await persist_progress()
            try:
                settlement_result = await refresh_settlements(
                    session, client, wallet_id=wallet.id, condition_ids=list(market_rows),
                )
            except (PolymarketAPIError, ValueError, OSError) as exc:
                termination = "upstream_error"
                logger.warning(
                    "wallet_bootstrap_settlement_error",
                    wallet=wallet.address,
                    error=str(exc),
                )
                break
            if settlement_result["errors"]:
                termination = "upstream_error"
                logger.warning(
                    "wallet_bootstrap_settlement_error",
                    wallet=wallet.address,
                    errors=settlement_result["errors"],
                )
                break
            if settlement_result["checked"] == len(market_rows):
                settlement_checked.extend(market_rows)

        local_count, settled_count, db_oldest, db_newest = await evidence()
        oldest = min(_aware_utc(oldest), _aware_utc(db_oldest)) if oldest and db_oldest else (db_oldest or oldest)
        newest = max(_aware_utc(newest), _aware_utc(db_newest)) if newest and db_newest else (db_newest or newest)
        mature = (
            local_count >= MIN_TRADES
            and settled_count >= MIN_SETTLED_MARKETS
            and oldest is not None
            and (now - _aware_utc(oldest)).days >= MIN_ACCOUNT_AGE_DAYS
        )
        if mature:
            termination = "maturity_satisfied"
            break
        if len(raw) < requested_limit:
            termination = "history_exhausted"
            break
        if requests >= max_requests or pages >= max_pages or rows_fetched >= max_trades:
            termination = "configured_limit_reached"
            break

    count, settled_count, db_oldest, db_newest = await evidence()
    oldest = min(_aware_utc(oldest), _aware_utc(db_oldest)) if oldest and db_oldest else (db_oldest or oldest)
    newest = max(_aware_utc(newest), _aware_utc(db_newest)) if newest and db_newest else (db_newest or newest)
    if (termination == "configured_limit_reached" and not mature
            and pages_this_run >= getattr(settings, "bootstrap_pages_per_run", max_pages)
            and pages < max_pages and rows_fetched < max_trades and requests < max_requests):
        termination = "in_progress"
    record = {
        "wallet": wallet.address,
        "wallet_id": wallet.id,
        "pages_requested": pages,
        "rows_fetched": rows_fetched,
        "new_trades_inserted": int(state.get("new_trades_inserted", 0)) + inserted_total,
        "duplicates_skipped": int(state.get("duplicates_skipped", 0)) + duplicates_total,
        "quarantined_rows": int(state.get("quarantined_rows", 0)) + quarantined_total,
        "oldest_trade_observed": oldest.isoformat() if oldest else None,
        "newest_trade_observed": newest.isoformat() if newest else None,
        "settled_market_count": settled_count,
        "total_local_trade_count": count,
        "maturity_satisfied": mature,
        "termination_reason": termination,
        "next_offset": offset,
        "end_timestamp": end_ts,
        "window_oldest": window_oldest,
        "requests_used": requests,
        "settlement_checked": settlement_checked,
    }
    session.add(DecisionLogEntry(actor="bot", action="wallet_historical_bootstrap", context=record))
    await session.commit()
    logger.info("wallet_historical_bootstrap", **record)
    return record


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
    return await ingest_trade_rows(
        session, client, wallet, raw_trades,
        fetch_market_metadata=fetch_market_metadata,
        log_event="ingest_wallet_complete",
    )


async def ingest_trade_rows(
    session: AsyncSession,
    client: PolymarketClient,
    wallet: Wallet,
    raw_trades: list[dict[str, Any]],
    *,
    fetch_market_metadata: bool = True,
    log_event: str = "ingest_wallet_complete",
) -> WalletIngestionResult:
    """Persist an already fetched page with the canonical dedup contract."""
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
        except Exception as exc:  # noqa: BLE001 - deliberate row quarantine boundary
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
        log_event,
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
            if wallet.approval_state == "approved":
                from polycopy.ingestion.catchup import ingest_approved_wallet

                result = await ingest_approved_wallet(session, client, wallet)
            else:
                result = await ingest_wallet_trades(session, client, wallet)
            results[wallet.address] = result.inserted
            quarantined_rows += result.quarantined
        except Exception as exc:
            failed_wallets += 1
            await session.rollback()
            # Rollback expires identity-map objects; reload the tracked
            # wallets before a later wallet or caller reads their state.
            for tracked in wallets:
                await session.refresh(tracked)
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
