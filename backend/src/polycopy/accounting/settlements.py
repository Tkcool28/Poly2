"""Settlement feed: detect resolved markets, record them once.

Resolution source: Gamma market objects. Settlement lookups explicitly request
``closed=true`` because the generic Gamma market listing is not a reliable
historical-resolution lookup. If an exact condition-ID lookup returns no row,
the refresher may retry by a token ID already observed on a trade for that
local market.

Every fallback remains fail-closed:
* Gamma's returned ``conditionId`` must exactly match the local market.
* A token fallback must contain the exact traded token in ``clobTokenIds``.
* The market must be closed and have exactly one price-1 outcome.
* Anything absent or ambiguous is skipped; no result is invented.

Successful historical lookups also backfill safe display/identity metadata on
existing Market rows (question, slug, outcomes, token map, active/closed).
One Settlement per market remains enforced by ``uq_settlements_market``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.ingestion.client import (
    PolymarketAPIError,
    PolymarketClient,
    clob_token_map,
)
from polycopy.logging_config import get_logger
from polycopy.models import ApiThrottle, Market, Settlement, Trade

logger = get_logger("polycopy.accounting.settlements")


def detect_winning_outcome(gamma_market: dict[str, Any]) -> str | None:
    """Strictly extract the winning outcome from a Gamma market object.

    Returns None unless the market is closed AND exactly one outcome has
    price 1. Ambiguity is never settled.
    """
    if not gamma_market.get("closed"):
        return None
    outcomes = gamma_market.get("outcomes") or []
    prices_raw = gamma_market.get("outcomePrices")
    if isinstance(prices_raw, str):
        import json

        try:
            prices_raw = json.loads(prices_raw)
        except ValueError:
            return None
    if not outcomes or not prices_raw or len(outcomes) != len(prices_raw):
        return None
    try:
        prices = [Decimal(str(p)) for p in prices_raw]
    except InvalidOperation:
        return None
    winners = [i for i, p in enumerate(prices) if p == 1]
    if len(winners) != 1:
        return None
    if any(p < 0 or p > 1 for p in prices):
        return None
    return str(outcomes[winners[0]])


async def _first_traded_asset_id(
    session: AsyncSession,
    market_id: int,
) -> str | None:
    """Return one non-empty token ID already observed for this local market."""
    asset_id = await session.scalar(
        select(Trade.asset_id)
        .where(
            Trade.market_id == market_id,
            Trade.asset_id.is_not(None),
            Trade.asset_id != "",
        )
        .limit(1)
    )
    return str(asset_id) if asset_id else None


async def _lookup_closed_gamma_market(
    session: AsyncSession,
    client: PolymarketClient,
    market: Market,
) -> tuple[dict[str, Any] | None, bool]:
    """Find a closed Gamma market by condition ID, then by observed token ID.

    Returns ``(gamma_market, used_token_fallback)``.
    """
    gamma = await client.get_gamma_market(market.condition_id, closed=True)
    if gamma is not None:
        return gamma, False

    asset_id = await _first_traded_asset_id(session, market.id)
    if not asset_id:
        return None, False

    gamma = await client.get_gamma_market_by_token(
        asset_id,
        expected_condition_id=market.condition_id,
        closed=True,
    )
    return gamma, gamma is not None


def _backfill_market_metadata(market: Market, gamma: dict[str, Any]) -> bool:
    """Backfill trustworthy Gamma metadata without changing settlement truth."""
    changed = False

    question = str(gamma.get("question") or "")
    if question and question != market.question:
        market.question = question
        changed = True

    slug = gamma.get("slug")
    if slug and slug != market.slug:
        market.slug = str(slug)
        changed = True

    outcomes = gamma.get("outcomes")
    if outcomes and outcomes != market.outcomes:
        market.outcomes = list(outcomes)
        changed = True

    token_map = clob_token_map(gamma) or None
    if token_map and token_map != market.clob_token_ids:
        market.clob_token_ids = token_map
        changed = True

    if "active" in gamma:
        active = bool(gamma.get("active"))
        if active != market.active:
            market.active = active
            changed = True

    if "closed" in gamma:
        closed = bool(gamma.get("closed"))
        if closed != market.closed:
            market.closed = closed
            changed = True

    return changed


async def gamma_backoff_active(session: AsyncSession, now: datetime) -> bool:
    throttle = await session.get(ApiThrottle, "gamma")
    if throttle is None:
        return False
    due = throttle.next_allowed_at
    return (due if due.tzinfo else due.replace(tzinfo=UTC)) > now


async def refresh_settlements(
    session: AsyncSession,
    client: PolymarketClient,
    *,
    wallet_id: int | None = None,
    max_markets: int | None = None,
    condition_ids: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Check tracked markets without Settlement rows for resolution.

    Settlement discovery explicitly requests Gamma's closed-market surface.
    An empty closed lookup means "not resolved here or not indexed", not a
    successful negative result; the counter exposes that state instead of
    silently swallowing it.

    Markets are checked sequentially and each persisted metadata/settlement
    update is committed independently.
    """
    now = now or datetime.now(UTC)
    settled_market_ids = select(Settlement.market_id)
    stmt = select(Market).where(
        Market.id.not_in(settled_market_ids),
        or_(Market.settlement_next_check_at.is_(None),
            Market.settlement_next_check_at <= now),
    )
    if condition_ids is not None:
        if not condition_ids:
            return {
                "checked": 0, "settled": 0, "skipped_ambiguous": 0,
                "not_found_or_open": 0, "token_fallback_hits": 0,
                "metadata_backfilled": 0, "errors": 0,
            }
        stmt = stmt.where(Market.condition_id.in_(condition_ids))
    if wallet_id is not None:
        stmt = stmt.join(Trade, Trade.market_id == Market.id).where(
            Trade.wallet_id == wallet_id
        ).group_by(Market.id).order_by(func.min(Trade.traded_at))
    if wallet_id is None:
        stmt = stmt.order_by(Market.settlement_next_check_at, Market.id)
    if max_markets is not None:
        stmt = stmt.limit(max_markets)
    stats = {
        "checked": 0,
        "settled": 0,
        "skipped_ambiguous": 0,
        "not_found_or_open": 0,
        "token_fallback_hits": 0,
        "metadata_backfilled": 0,
        "errors": 0,
    }
    if await gamma_backoff_active(session, now):
        return stats
    throttle = await session.get(ApiThrottle, "gamma")
    markets = (await session.execute(stmt)).scalars().unique().all()
    for market in markets:
        stats["checked"] += 1
        market.settlement_last_checked_at = now
        market.settlement_attempt_count = (market.settlement_attempt_count or 0) + 1
        delay = min(12 * 3600, 60 * (2 ** min(market.settlement_attempt_count - 1, 10)))
        market.settlement_next_check_at = now + timedelta(seconds=delay)
        try:
            gamma, used_token_fallback = await _lookup_closed_gamma_market(
                session, client, market
            )
        except (PolymarketAPIError, ValueError) as exc:
            stats["errors"] += 1
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                market.settlement_next_check_at = now + timedelta(seconds=max(delay, retry_after))
                if throttle is None:
                    throttle = ApiThrottle(family="gamma", next_allowed_at=now)
                    session.add(throttle)
                throttle.next_allowed_at = max(
                    throttle.next_allowed_at.replace(tzinfo=UTC)
                    if throttle.next_allowed_at.tzinfo is None
                    else throttle.next_allowed_at,
                    now + timedelta(seconds=retry_after),
                )
            await session.commit()
            logger.warning(
                "settlement_check_failed",
                condition_id=market.condition_id,
                error=str(exc),
            )
            if retry_after is not None:
                # One 429 cools down this entire request family in the shared
                # bot client; avoid hammering subsequent markets this cycle.
                break
            continue

        if gamma is None:
            stats["not_found_or_open"] += 1
            logger.debug(
                "settlement_market_not_found_or_open",
                condition_id=market.condition_id,
            )
            await session.commit()
            continue

        if used_token_fallback:
            stats["token_fallback_hits"] += 1

        metadata_changed = _backfill_market_metadata(market, gamma)
        if metadata_changed:
            stats["metadata_backfilled"] += 1

        winner = detect_winning_outcome(gamma)
        if winner is None:
            if gamma.get("closed"):
                stats["skipped_ambiguous"] += 1
                logger.warning(
                    "settlement_ambiguous",
                    condition_id=market.condition_id,
                    outcomes=gamma.get("outcomes"),
                    outcomePrices=gamma.get("outcomePrices"),
                )
            await session.commit()
            continue

        market.closed = True
        market.resolved_outcome = winner
        market.settlement_next_check_at = None
        session.add(Settlement(market_id=market.id, winning_outcome=winner))
        await session.commit()
        stats["settled"] += 1
        logger.info(
            "market_settled",
            condition_id=market.condition_id,
            winner=winner,
            token_fallback=used_token_fallback,
        )
    return stats
