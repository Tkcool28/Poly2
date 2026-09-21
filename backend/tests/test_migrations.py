"""PostgreSQL migration regression for a populated pre-PR-E schema.

PostgreSQL is Poly2's authoritative database. The test is skipped only when
POLYCOPY_TEST_POSTGRES_URL is absent; CI provides a real PostgreSQL service.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config

from alembic import command
from polycopy.config import get_settings

BACKEND = Path(__file__).resolve().parent.parent


def _cfg(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("POLYCOPY_DATABASE_URL", db_url)
    get_settings.cache_clear()
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _seed(db_url: str) -> None:
    conn = await asyncpg.connect(_dsn(db_url))
    try:
        await conn.execute(
            "INSERT INTO wallets (address, approval_state, is_sample, created_at, updated_at) "
            "VALUES ('0xlegacy', 'approved', false, "
            "'2026-09-01 00:00:00+00', '2026-09-10 12:00:00+00')"
        )
        await conn.execute(
            "INSERT INTO markets (condition_id, question, active, closed, created_at) "
            "VALUES ('0xcond', '?', true, false, '2026-09-01 00:00:00+00')"
        )
        await conn.execute(
            "INSERT INTO trades (polymarket_trade_id, market_id, wallet_id, asset_id, "
            "side, outcome, size, price, fee, traded_at, ingested_at) "
            "VALUES ('data-api:0xtx:0xlegacy:4667:5:0.5:1', 1, 1, '4667', "
            "'BUY', 'Up', 5, 0.5, 0, '2026-09-10 00:00:00+00', "
            "'2026-09-10 00:00:00+00')"
        )
        await conn.execute(
            "INSERT INTO signals (wallet_id, market_id, side, outcome, status, created_at) "
            "VALUES (1, 1, 'BUY', 'Up', 'pending', '2026-09-10 00:00:00+00')"
        )
        await conn.execute(
            "INSERT INTO positions (market_id, outcome, quantity, avg_price, realized_pnl, "
            "unrealized_pnl, updated_at) VALUES "
            "(1, 'Up', 3, 0.5, 0, 0, '2026-09-10 00:00:00+00')"
        )
    finally:
        await conn.close()


async def _verify(db_url: str) -> None:
    conn = await asyncpg.connect(_dsn(db_url))
    try:
        assert await conn.fetchval("SELECT COUNT(*) FROM signals") == 0
        assert await conn.fetchval(
            "SELECT is_nullable = 'NO' FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='signals' "
            "AND column_name='source_trade_id'"
        )
        assert await conn.fetchval(
            "SELECT approved_at IS NOT NULL FROM wallets WHERE address='0xlegacy'"
        )
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='positions'"
            )
        }
        assert {"wallet_id", "settled_at"} <= cols
        assert await conn.fetchval("SELECT COUNT(*) FROM positions") == 0
        constraints = {
            r["constraint_name"]
            for r in await conn.fetch(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema='public' AND table_name='positions'"
            )
        }
        assert "uq_positions_wallet_market_outcome" in constraints
        assert "fk_positions_wallet_id_wallets" in constraints
    finally:
        await conn.close()


def test_upgrade_from_populated_pre_pr_e_schema_postgres(
    monkeypatch: pytest.MonkeyPatch,
):
    db_url = os.environ.get("POLYCOPY_TEST_POSTGRES_URL")
    if not db_url:
        pytest.skip("requires POLYCOPY_TEST_POSTGRES_URL")

    cfg = _cfg(db_url, monkeypatch)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "0002")
    asyncio.run(_seed(db_url))
    command.upgrade(cfg, "head")
    asyncio.run(_verify(db_url))
    get_settings.cache_clear()
