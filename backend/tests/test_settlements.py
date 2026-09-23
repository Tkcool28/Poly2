"""Settlement feed: strict winner detection, fail-closed writes."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from polycopy.accounting.settlements import detect_winning_outcome, refresh_settlements
from polycopy.ingestion.client import PolymarketClient
from polycopy.models import Base, Market, Settlement

# --- Pure detection logic -------------------------------------------------

_DEFAULT = object()


def _gamma(closed=True, outcomes=None, prices=_DEFAULT):
    return {
        "closed": closed,
        "outcomes": outcomes if outcomes is not None else ["Up", "Down"],
        "outcomePrices": prices if prices is not _DEFAULT else '["1", "0"]',
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
        '["0.5", "0.5"]',  # not resolved yet, mid prices
        '["1", "1"]',  # corrupt: two winners
        '["0", "0"]',  # corrupt: no winner
        '["0.99", "0.01"]',  # near but not final
        "not json",  # garbage
        None,  # missing
    ],
)
def test_ambiguous_prices_never_settle(prices):
    assert detect_winning_outcome(_gamma(prices=prices)) is None


def test_length_mismatch_never_settles():
    assert detect_winning_outcome(_gamma(outcomes=["Up"], prices='["1", "0"]')) is None


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
    def handler(request: httpx.Request) -> httpx.Response:
        cond = request.url.params.get("condition_ids", "")
        market = markets_by_condition.get(cond)
        return httpx.Response(200, json=[market] if market else [])

    transport = httpx.MockTransport(handler)
    return PolymarketClient(http_client=httpx.AsyncClient(transport=transport))


async def _add_market(session, condition_id, closed=False):
    m = Market(condition_id=condition_id, question="?", closed=closed)
    session.add(m)
    await session.commit()
    return m


async def test_refresh_settles_closed_market(session):
    await _add_market(session, "0xcond1")
    client = _client_with({"0xcond1": _gamma()})
    stats = await refresh_settlements(session, client)
    assert stats == {"checked": 1, "settled": 1, "skipped_ambiguous": 0, "errors": 0}

    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.winning_outcome == "Up"
    market = (await session.execute(select(Market))).scalar_one()
    assert market.closed is True
    assert market.resolved_outcome == "Up"


async def test_refresh_skips_open_and_ambiguous(session):
    await _add_market(session, "0xopen")
    await _add_market(session, "0xambig")
    client = _client_with(
        {
            "0xopen": _gamma(closed=False),
            "0xambig": _gamma(prices='["1", "1"]'),
        }
    )
    stats = await refresh_settlements(session, client)
    assert stats["settled"] == 0
    assert stats["skipped_ambiguous"] == 1
    assert (await session.scalar(select(func.count(Settlement.id)))) == 0


async def test_refresh_is_idempotent(session):
    await _add_market(session, "0xcond1")
    client = _client_with({"0xcond1": _gamma()})
    await refresh_settlements(session, client)
    stats = await refresh_settlements(session, client)
    assert stats["checked"] == 0  # already settled → not re-queried
    assert (await session.scalar(select(func.count(Settlement.id)))) == 1


async def test_refresh_settles_historical_closed_market_without_settlement_row(session):
    """REVIEW REGRESSION: a market ingested with closed=True from Gamma
    metadata (historical market, traded before we tracked it) has no
    Settlement row. It must REMAIN eligible for settlement refresh —
    filtering on Market.closed would orphan its paper positions forever.
    """
    await _add_market(session, "0xhist", closed=True)
    client = _client_with({"0xhist": _gamma()})
    stats = await refresh_settlements(session, client)
    assert stats["settled"] == 1
    settlement = (await session.execute(select(Settlement))).scalar_one()
    assert settlement.winning_outcome == "Up"


async def test_refresh_closed_market_ambiguous_winner_is_skipped(session):
    """closed=True locally but Gamma's winner data is ambiguous → skip,
    never guess. (Covers the newly-eligible closed market path.)"""
    await _add_market(session, "0xambig", closed=True)
    client = _client_with({"0xambig": _gamma(prices='["1", "1"]')})
    stats = await refresh_settlements(session, client)
    assert stats["settled"] == 0
    assert stats["skipped_ambiguous"] == 1
    assert (await session.scalar(select(func.count(Settlement.id)))) == 0


async def test_gamma_error_does_not_stop_other_markets(session):
    await _add_market(session, "0xbad")
    await _add_market(session, "0xgood")

    def handler(request: httpx.Request) -> httpx.Response:
        cond = request.url.params.get("condition_ids", "")
        if cond == "0xbad":
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=[_gamma()])

    transport = httpx.MockTransport(handler)
    client = PolymarketClient(http_client=httpx.AsyncClient(transport=transport))
    stats = await refresh_settlements(session, client)
    assert stats["errors"] == 1
    assert stats["settled"] == 1
