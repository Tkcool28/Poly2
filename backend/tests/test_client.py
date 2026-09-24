"""Polymarket client behavior: parsing quirks, retries, concurrency cap."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from polycopy.ingestion.client import (
    DATA_API_BASE,
    PolymarketAPIError,
    PolymarketClient,
    clob_token_map,
)


def _client(handler, **kwargs) -> PolymarketClient:
    transport = httpx.MockTransport(handler)
    return PolymarketClient(
        http_client=httpx.AsyncClient(transport=transport), **kwargs
    )


async def test_get_trades_hits_data_api():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/trades"
        assert request.url.params["user"] == "0xabc"
        return httpx.Response(200, json=[{"transactionHash": "0x1"}])

    async with _client(handler) as client:
        trades = await client.get_trades("0xabc", limit=10)
    assert trades == [{"transactionHash": "0x1"}]


async def test_gamma_market_parses_json_encoded_strings():
    """Gamma returns clobTokenIds/outcomes as JSON strings."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gamma-api.polymarket.com"
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": "0xcond",
                    "question": "Will it rain?",
                    "slug": "will-it-rain",
                    "clobTokenIds": '["4667", "8761"]',
                    "outcomes": '["Up", "Down"]',
                }
            ],
        )

    async with _client(handler) as client:
        market = await client.get_gamma_market("0xcond")
    assert market["clobTokenIds"] == ["4667", "8761"]
    assert market["outcomes"] == ["Up", "Down"]
    assert clob_token_map(market) == {"Up": "4667", "Down": "8761"}


async def test_gamma_closed_lookup_sends_explicit_closed_filter():
    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": "0xhist",
                    "closed": True,
                    "clobTokenIds": '["1", "2"]',
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["1", "0"]',
                }
            ],
        )

    async with _client(handler) as client:
        market = await client.get_gamma_market("0xhist", closed=True)

    assert seen_params["condition_ids"] == "0xhist"
    assert seen_params["closed"] == "true"
    assert market is not None
    assert market["outcomePrices"] == ["1", "0"]


async def test_gamma_market_requires_exact_condition_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": "0xwrong",
                    "clobTokenIds": '["11", "12"]',
                    "outcomes": '["Yes", "No"]',
                },
                {
                    "conditionId": "0xright",
                    "clobTokenIds": '["21", "22"]',
                    "outcomes": '["Up", "Down"]',
                },
            ],
        )

    async with _client(handler) as client:
        market = await client.get_gamma_market("0xright")

    assert market is not None
    assert market["conditionId"] == "0xright"
    assert market["clobTokenIds"] == ["21", "22"]


async def test_gamma_market_none_when_filter_returns_only_wrong_identity():
    async with _client(
        lambda r: httpx.Response(
            200,
            json=[
                {
                    "conditionId": "0xother",
                    "clobTokenIds": '["1", "2"]',
                    "outcomes": '["Yes", "No"]',
                }
            ],
        )
    ) as client:
        assert await client.get_gamma_market("0xmissing") is None


async def test_gamma_market_none_when_empty():
    async with _client(lambda r: httpx.Response(200, json=[])) as client:
        assert await client.get_gamma_market("0xmissing") is None


async def test_gamma_token_fallback_requires_token_and_condition_match():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["clob_token_ids"] == "777"
        assert request.url.params["closed"] == "true"
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": "0xwrong",
                    "clobTokenIds": '["777", "778"]',
                    "outcomes": '["Yes", "No"]',
                },
                {
                    "conditionId": "0xright",
                    "clobTokenIds": '["777", "999"]',
                    "outcomes": '["Up", "Down"]',
                    "closed": True,
                    "outcomePrices": '["1", "0"]',
                },
            ],
        )

    async with _client(handler) as client:
        market = await client.get_gamma_market_by_token(
            "777",
            expected_condition_id="0xright",
            closed=True,
        )

    assert market is not None
    assert market["conditionId"] == "0xright"
    assert market["clobTokenIds"] == ["777", "999"]


async def test_retries_on_429_then_succeeds(monkeypatch):
    async def no_sleep(*_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=[])

    async with _client(handler) as client:
        assert await client.get_positions("0xabc") == []
    assert calls == 3


async def test_non_retryable_4xx_raises_immediately():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="not found")

    async with _client(handler) as client:
        with pytest.raises(PolymarketAPIError, match="non-retryable 404"):
            await client.get_positions("0xabc")
    assert calls == 1


async def test_exhausted_retries_raise(monkeypatch):
    async def no_sleep(*_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async with _client(lambda r: httpx.Response(503, text="down")) as client:
        with pytest.raises(PolymarketAPIError, match="failed after 3 attempts"):
            await client.get_positions("0xabc")


async def test_concurrency_is_capped():
    in_flight = 0
    peak = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with _client(handler, max_concurrent=2) as client:
        original_get = client._client.get

        async def counting_get(*args, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            try:
                return await original_get(*args, **kwargs)
            finally:
                in_flight -= 1

        client._client.get = counting_get
        urls = [f"{DATA_API_BASE}/positions?user=0x{i}" for i in range(6)]
        await asyncio.gather(*[client._get(u) for u in urls])
    assert peak <= 2
