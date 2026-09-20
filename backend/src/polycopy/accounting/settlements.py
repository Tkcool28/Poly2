"""Settlement feed: detect resolved markets, record them once.

Resolution source: Gamma market objects. When a market resolves, Gamma
sets ``closed=true`` and its ``outcomePrices`` collapse to the winner —
e.g. ``outcomes=["Up","Down"]``, ``outcomePrices=["1","0"]`` means "Up"
won. (``outcomePrices`` is a JSON-encoded string, same quirk as
``clobTokenIds``.)

⚠ HONESTY NOTE: the outcomePrices collapse behavior is standard
Polymarket behavior but has NOT yet been probe-verified against a
freshly resolved market from an unrestricted network. The parsing is
strict (anything ambiguous → market skipped, logged, NOT settled) so a
wrong guess can never write a wrong settlement. Verification via the
probe script is tracked in docs/source-identity-contract.md.

Fail-closed rules:
* Not closed → skip.
* Closed but no unambiguous winner (no price exactly "1", or two "1"s)
  → skip and log. Never invent a result.
* One Settlement per market, enforced by the uq_settlements_market
  constraint; re-runs are no-ops.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.ingestion.client import PolymarketAPIError, PolymarketClient
from polycopy.logging_config import get_logger
from polycopy.models import Market, Settlement

logger = get_logger("polycopy.accounting.settlements")


def detect_winning_outcome(gamma_market: dict[str, Any]) -> str | None:
    """Strictly extract the winning outcome from a Gamma market object.

    Returns None unless the market is closed AND exactly one outcome has
    price 1 (and all others are 0-ish). Ambiguity is never settled.
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


async def refresh_settlements(
    session: AsyncSession,
    client: PolymarketClient,
) -> dict[str, int]:
    """Check tracked open markets for resolution; record new settlements.

    Markets are checked SEQUENTIALLY (client caps concurrency anyway) and
    each market's settlement is its own transaction. Returns counters.
    """
    settled_market_ids = select(Settlement.market_id)
    markets = (
        (
            await session.execute(
                select(Market).where(
                    Market.closed.is_(False),
                    Market.id.not_in(settled_market_ids),
                )
            )
        )
        .scalars()
        .all()
    )

    stats = {"checked": 0, "settled": 0, "skipped_ambiguous": 0, "errors": 0}
    for market in markets:
        stats["checked"] += 1
        try:
            gamma = await client.get_gamma_market(market.condition_id)
        except (PolymarketAPIError, ValueError) as exc:
            stats["errors"] += 1
            logger.warning(
                "settlement_check_failed",
                condition_id=market.condition_id,
                error=str(exc),
            )
            continue
        if gamma is None:
            continue

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
            continue

        market.closed = True
        market.resolved_outcome = winner
        session.add(Settlement(market_id=market.id, winning_outcome=winner))
        await session.commit()
        stats["settled"] += 1
        logger.info(
            "market_settled", condition_id=market.condition_id, winner=winner
        )
    return stats
