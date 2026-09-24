"""Polymarket API client — bounded, retried, and parse-aware.

One client instance owns:

* a global asyncio semaphore capping concurrent in-flight requests
  (``ingestion_max_concurrent_requests``, default 4) — this is the primary
  defense against the "many calls at once ate the box" failure mode;
* retry-with-backoff on 429 / 5xx / transport errors (bounded attempts);
* response parsing for known API quirks, notably Gamma's ``clobTokenIds``
  and ``outcomes`` fields, which are JSON-encoded strings.

Endpoints:
* Data API ``GET /trades?user=`` — trade history for a wallet
* Data API ``GET /positions?user=`` — position reconciliation
* Gamma API ``GET /markets?condition_ids=`` — market metadata + token map
* Gamma API ``GET /markets?clob_token_ids=`` — token-identity fallback
* CLOB API ``GET /markets/{condition_id}`` — token map fallback
* CLOB API ``GET /book?token_id=`` — order book for paper fills (PR-E)

Gamma historical-market note: resolved markets must be requested explicitly
with ``closed=true`` when doing settlement discovery. Callers that need
resolution data should pass ``closed=True``; a generic market lookup is not
assumed to include historical/closed rows.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self

import httpx

from polycopy.config import get_settings
from polycopy.logging_config import get_logger

DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 0.5


class PolymarketAPIError(Exception):
    """Raised when an endpoint fails after all retries."""


class PolymarketClient:
    """Async client with a global concurrency cap and bounded retries."""

    def __init__(
        self,
        *,
        max_concurrent: int | None = None,
        timeout_seconds: float = 20.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._max_concurrent = (
            max_concurrent
            if max_concurrent is not None
            else settings.ingestion_max_concurrent_requests
        )
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = http_client is None
        self._logger = get_logger("polycopy.ingestion.client")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET with concurrency cap + bounded retry/backoff. Fail-closed."""
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            async with self._semaphore:
                try:
                    resp = await self._client.get(url, params=params)
                except httpx.HTTPError as exc:
                    last_error = exc
                    self._logger.warning(
                        "api_transport_error", url=url, attempt=attempt, error=str(exc)
                    )
                else:
                    if resp.status_code == 200:
                        return resp.json()
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_error = PolymarketAPIError(
                            f"{url} returned {resp.status_code}"
                        )
                        self._logger.warning(
                            "api_retryable_status",
                            url=url,
                            status=resp.status_code,
                            attempt=attempt,
                        )
                    else:
                        # 4xx (non-429) is a caller bug or bad input — no retry.
                        raise PolymarketAPIError(
                            f"{url} returned non-retryable {resp.status_code}: "
                            f"{resp.text[:200]}"
                        )
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
        raise PolymarketAPIError(f"{url} failed after {_MAX_RETRIES} attempts: {last_error}")

    # --- Data API --------------------------------------------------------

    async def get_trades(self, wallet: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Trade history for one wallet, most recent first.

        ``limit`` is hard-capped by the caller (service enforces the
        configured batch size); this method never paginates beyond one
        request.
        """
        return await self._get(
            f"{DATA_API_BASE}/trades", params={"user": wallet, "limit": limit}
        )

    async def get_positions(self, wallet: str) -> list[dict[str, Any]]:
        """Current positions for reconciliation."""
        return await self._get(f"{DATA_API_BASE}/positions", params={"user": wallet})

    # --- Gamma API -------------------------------------------------------

    @staticmethod
    def _parse_json_string(value: Any) -> Any:
        """Gamma returns clobTokenIds/outcomes/outcomePrices as JSON strings."""
        if isinstance(value, str):
            return json.loads(value)
        return value

    def _parse_gamma_market(self, raw_market: dict[str, Any]) -> dict[str, Any]:
        """Return a parsed copy of one Gamma market object."""
        market = dict(raw_market)
        market["clobTokenIds"] = self._parse_json_string(market.get("clobTokenIds"))
        market["outcomes"] = self._parse_json_string(market.get("outcomes"))
        market["outcomePrices"] = self._parse_json_string(market.get("outcomePrices"))
        return market

    @staticmethod
    def _same_condition(left: Any, right: str) -> bool:
        return str(left or "").lower() == right.lower()

    async def get_gamma_market(
        self,
        condition_id: str,
        *,
        closed: bool | None = None,
    ) -> dict[str, Any] | None:
        """Fetch one exact Gamma market by condition ID.

        ``closed=True`` is required by settlement callers so historical
        resolved markets are not hidden by the generic listing behavior.

        The response is identity-checked. We never trust ``markets[0]``
        unless its ``conditionId`` exactly matches the requested condition.
        """
        params: dict[str, Any] = {"condition_ids": condition_id}
        if closed is not None:
            params["closed"] = str(closed).lower()
        markets = await self._get(f"{GAMMA_API_BASE}/markets", params=params)
        if not isinstance(markets, list):
            return None
        for raw_market in markets:
            if isinstance(raw_market, dict) and self._same_condition(
                raw_market.get("conditionId"), condition_id
            ):
                return self._parse_gamma_market(raw_market)
        return None

    async def get_gamma_market_by_token(
        self,
        token_id: str,
        *,
        expected_condition_id: str,
        closed: bool | None = None,
    ) -> dict[str, Any] | None:
        """Fallback lookup by traded token ID, still enforcing condition identity.

        The token must be present in the returned ``clobTokenIds``, and the
        returned market's condition ID must exactly match
        ``expected_condition_id``. This prevents a fallback lookup from
        silently settling the wrong local market.
        """
        params: dict[str, Any] = {"clob_token_ids": token_id}
        if closed is not None:
            params["closed"] = str(closed).lower()
        markets = await self._get(f"{GAMMA_API_BASE}/markets", params=params)
        if not isinstance(markets, list):
            return None
        for raw_market in markets:
            if not isinstance(raw_market, dict):
                continue
            if not self._same_condition(
                raw_market.get("conditionId"), expected_condition_id
            ):
                continue
            market = self._parse_gamma_market(raw_market)
            token_ids = {str(value) for value in (market.get("clobTokenIds") or [])}
            if str(token_id) in token_ids:
                return market
        return None

    async def get_clob_market(self, condition_id: str) -> dict[str, Any]:
        """CLOB market object (token mapping fallback)."""
        return await self._get(f"{CLOB_API_BASE}/markets/{condition_id}")

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        """CLOB order book for one token: {"bids": [...], "asks": [...]}.

        Each level is {"price": "0.52", "size": "123.4"}. One request per
        signal at decision time — this is what realistic paper fills are
        priced against (docs/paper-execution-model.md).
        """
        return await self._get(f"{CLOB_API_BASE}/book", params={"token_id": token_id})


def clob_token_map(gamma_market: dict[str, Any]) -> dict[str, str]:
    """Build {outcome: clob_token_id} from a parsed Gamma market object."""
    outcomes = gamma_market.get("outcomes") or []
    token_ids = gamma_market.get("clobTokenIds") or []
    return {str(o): str(t) for o, t in zip(outcomes, token_ids, strict=False)}
