"""Settlement feed: historical lookup, strict winner detection, fail-closed writes."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.accounting.settlements import detect_winning_outcome, refresh_settlements
from polycopy.ingestion.client import PolymarketClient
from polycopy.models import ApiThrottle, Base, Market, Settlement, Trade, Wallet

# --- Pure detection logic -------------------------------------------------

_DEFAULT = object()


def _gamma(
    condition_id="0xcond",
    *,
    closed=True,
    outcomes=None,
    prices=_DEFAULT,
    tokens=None,
    question="Recovered historical question",
    slug="recovered-historical-market",
):
    return {
        "conditionId": condition_id,
        "question": question,
        "slug": slug,
        "closed": closed,
        "active": not closed,
        "outcomes": outcomes if outcomes is not None else ["Up", "Down"],
        "outcomePrices": prices if prices is not _DEFAULT else '["1", "0"]',
        "clobTokenIds": tokens if tokens is not None else ["4667", "8761"],
    }


def test_detects_winner_from_json_string_prices():
    assert detect_winning_outcome(_gamma()) == "Up"


def test_detects_second_outcome_winner():
    assert detect_winning_outcome(_gamma(prices=["0", "1"])) == "Down"


def test_open_market_is_never_settled():
    assert detect_winning_outcome(_gamma(closed=False)) is None


@pytest.mark.parametrize(
    "prices",
    [
        '["0.5", "0.5"]',
        '["1", "1"]',
        '["0", "0"]',
        '["0.99", "0.01"]',
        "not json",
        None,
    ],
)
def test_ambiguous_prices_never_settle(prices):
    assert detect_winning_outcome(_gamma(prices=prices)) is None


def test_length_mismatch_never_settles():
    assert detect_winning_outcome(
        _gamma(outcomes=["Up"], prices='["1", "0"]')
    ) is None


# --- DB-backed refresh ----------------------------------------------------


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    async def no_sleep(*_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _client_with(markets_by_condition: dict[str, dict]) -> PolymarketClient:
    """Simulate Gamma's historical behavior: closed rows require closed=true."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gamma-api.polymarket.com"
        assert request.url.params.get("closed") == "true"
        cond = request.url.params.get("condition_ids", "")
        if cond:
            market = markets_by_condition.get(cond)
            if market and market.get("closed"):
                return httpx.Response(200, json=[market])
            return httpx.Response(200, json=[])

        token_id = request.url.params.get("clob_token_ids", "")
        for market in markets_by_condition.values():
            if (
                market.get("closed")
                and token_id
                and token_id in {str(v) for v in market.get("clobTokenIds", [])}
            ):
                return httpx.Response(200, json=[market])
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    return PolymarketClient(http_client=httpx.AsyncClient(transport=transport))


async def _add_market(session, condition_id, closed=False):
    m = Market(condition_id=condition_id, question="?", closed=closed)
    session.add(m)
    await session.commit()
    return m


async def _add_trade_for_market(session, market: Market, asset_id: str):
    wallet = Wallet(address="0x" + "a" * 40, approval_state="discovered")
    session.add(wallet)
    await session.flush()
    trade = Trade(
        polymarket_trade_id=f"test:{market.condition_id}:{asset_id}",
        market_id=market.id,
        wallet_id=wallet.id,
        asset_id=asset_id,
        side="BUY",
        outcome="Up",
        size=1,
        price=0.5,
        fee=0,
        traded_at=market.created_at,
    )
    session.add(trade)
    await session.commit()


async def test_refresh_settles_closed_market_with_explicit_historical_lookup(session):
    await _add_market(session, "0xcond1")
    client = _client_with({"0xcond1": _gamma("0xcond1")})
    stats = await refresh_settlements(session, client)

    assert stats == {
        "checked": 1,
        "settled": 1,
        "skipped_ambiguous": 0,
        "not_found_or_open": 0,
        "token_fallback_hits": 0,
        "metadata_backfilled": 1,
        "errors": 0,
    }

    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.winning_outcome == "Up"
    market = (await session.execute(select(Market))).scalar_one()
    assert market.closed is True
    assert market.resolved_outcome == "Up"
    assert market.question == "Recovered historical question"
    assert market.slug == "recovered-historical-market"
    assert market.outcomes == ["Up", "Down"]
    assert market.clob_token_ids == {"Up": "4667", "Down": "8761"}


async def test_refresh_open_market_is_visible_as_not_found_or_open(session):
    await _add_market(session, "0xopen")
    client = _client_with({"0xopen": _gamma("0xopen", closed=False)})
    stats = await refresh_settlements(session, client)

    assert stats["settled"] == 0
    assert stats["not_found_or_open"] == 1
    assert stats["skipped_ambiguous"] == 0
    assert (await session.scalar(select(func.count(Settlement.id)))) == 0


async def test_refresh_closed_ambiguous_market_is_skipped(session):
    await _add_market(session, "0xambig")
    client = _client_with(
        {"0xambig": _gamma("0xambig", prices='["1", "1"]')}
    )
    stats = await refresh_settlements(session, client)

    assert stats["settled"] == 0
    assert stats["skipped_ambiguous"] == 1
    assert stats["metadata_backfilled"] == 1
    assert (await session.scalar(select(func.count(Settlement.id)))) == 0


async def test_refresh_is_idempotent(session):
    await _add_market(session, "0xcond1")
    client = _client_with({"0xcond1": _gamma("0xcond1")})
    await refresh_settlements(session, client)
    stats = await refresh_settlements(session, client)

    assert stats["checked"] == 0
    assert (await session.scalar(select(func.count(Settlement.id)))) == 1


async def test_unresolved_market_has_persisted_due_time_and_backoff(session):
    market = await _add_market(session, "0xopen")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[_gamma("0xopen", closed=False)])

    client = PolymarketClient(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    t0 = datetime(2026, 9, 24, tzinfo=UTC)
    assert (await refresh_settlements(session, client, now=t0))["checked"] == 1
    assert market.settlement_attempt_count == 1
    assert market.settlement_next_check_at.replace(tzinfo=UTC) == t0 + timedelta(seconds=60)
    assert (await refresh_settlements(session, client, now=t0 + timedelta(seconds=30)))["checked"] == 0
    assert calls == 1
    assert (await refresh_settlements(session, client, now=t0 + timedelta(seconds=60)))["checked"] == 1
    assert market.settlement_attempt_count == 2
    assert market.settlement_next_check_at.replace(tzinfo=UTC) == t0 + timedelta(seconds=180)
    assert calls == 2


async def test_gamma_429_cools_request_family_and_recovers_without_false_settlement(session):
    market = await _add_market(session, "0xrate")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json=[_gamma("0xrate")])

    client = PolymarketClient(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    t0 = datetime(2026, 9, 24, tzinfo=UTC)
    stats = await refresh_settlements(session, client, now=t0)
    assert stats["errors"] == 1
    assert calls == 1
    assert market.settlement_next_check_at.replace(tzinfo=UTC) == t0 + timedelta(seconds=120)
    assert (await refresh_settlements(session, client, now=t0 + timedelta(seconds=30)))["checked"] == 0
    assert (await session.get(ApiThrottle, "gamma")).next_allowed_at.replace(tzinfo=UTC) == t0 + timedelta(seconds=120)
    new_client = PolymarketClient(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await refresh_settlements(session, new_client, now=t0 + timedelta(seconds=60)))["checked"] == 0
    assert calls == 1
    client._gamma_backoff_until = 0  # simulated expiry of shared client cooldown
    stats = await refresh_settlements(session, client, now=t0 + timedelta(seconds=120))
    assert stats["settled"] == 1
    assert calls == 2
    assert (await session.scalar(select(func.count(Settlement.id)))) == 1


async def test_refresh_settles_historical_closed_market_without_settlement_row(session):
    await _add_market(session, "0xhist", closed=True)
    client = _client_with({"0xhist": _gamma("0xhist")})
    stats = await refresh_settlements(session, client)

    assert stats["settled"] == 1
    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.winning_outcome == "Up"


async def test_refresh_falls_back_to_observed_asset_id(session):
    market = await _add_market(session, "0xhist")
    await _add_trade_for_market(session, market, "777")

    gamma = _gamma(
        "0xhist",
        tokens=["777", "888"],
        prices='["0", "1"]',
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("closed") == "true"
        if request.url.params.get("condition_ids"):
            return httpx.Response(200, json=[])
        assert request.url.params.get("clob_token_ids") == "777"
        return httpx.Response(200, json=[gamma])

    transport = httpx.MockTransport(handler)
    client = PolymarketClient(http_client=httpx.AsyncClient(transport=transport))
    stats = await refresh_settlements(session, client)

    assert stats["settled"] == 1
    assert stats["token_fallback_hits"] == 1
    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.winning_outcome == "Down"


async def test_token_fallback_wrong_condition_never_settles(session):
    market = await _add_market(session, "0xexpected")
    await _add_trade_for_market(session, market, "777")

    wrong = _gamma("0xwrong", tokens=["777", "888"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("condition_ids"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[wrong])

    transport = httpx.MockTransport(handler)
    client = PolymarketClient(http_client=httpx.AsyncClient(transport=transport))
    stats = await refresh_settlements(session, client)

    assert stats["settled"] == 0
    assert stats["token_fallback_hits"] == 0
    assert stats["not_found_or_open"] == 1
    assert (await session.scalar(select(func.count(Settlement.id)))) == 0


async def test_gamma_error_does_not_stop_other_markets(session):
    await _add_market(session, "0xbad")
    await _add_market(session, "0xgood")

    def handler(request: httpx.Request) -> httpx.Response:
        cond = request.url.params.get("condition_ids", "")
        if cond == "0xbad":
            return httpx.Response(503, text="down")
        if cond == "0xgood":
            return httpx.Response(200, json=[_gamma("0xgood")])
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = PolymarketClient(http_client=httpx.AsyncClient(transport=transport))
    stats = await refresh_settlements(session, client)

    assert stats["errors"] == 1
    assert stats["settled"] == 1
