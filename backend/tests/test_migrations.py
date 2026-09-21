"""PostgreSQL migration regression for the populated pre-PR-E schema.

PostgreSQL is Poly2's authoritative database. This test is skipped only when
POLYCOPY_TEST_POSTGRES_URL is not provided (for lightweight local runs); CI
provides a real PostgreSQL service and therefore always executes it.
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


def _alembic_cfg(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("POLYCOPY_DATABASE_URL", db_url)
    get_settings.cache_clear()
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


def _asyncpg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _seed_pre_pr_e(db_url: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(db_url))
    try:
        await conn.execute(
            """
            INSERT INTO wallets
                (address, approval_state, is_sample, created_at, updated_at)
            VALUES
                ('0xlegacy', 'approved', false,
                 '2026-09-01 00:00:00+00', '2026-09-10 12:00:00+00')
            """
        )
        await conn.execute(
            """
            INSERT INTO markets
                (condition_id, question, active, closed, created_at)
            VALUES
                ('0xcond', '?', true, false, '2026-09-01 00:00:00+00')
            """
        )
        await conn.execute(
            """
            INSERT INTO trades
                (polymarket_trade_id, market_id, wallet_id, asset_id,
                 side, outcome, size, price, fee, traded_at, ingested_at)
            VALUES
                ('data-api:0xtx:0xlegacy:4667:5:0.5:1',
                 1, 1, '4667', 'BUY', 'Up', 5, 0.5, 0,
                 '2026-09-10 00:00:00+00', '2026-09-10 00:00:00+00')
            """
        )
        # Legacy placeholders: neither row has enough identity to be trusted
        # as PR-E execution evidence.
        await conn.execute(
            """
            INSERT INTO signals
                (wallet_id, market_id, side, outcome, status, created_at)
            VALUES
                (1, 1, 'BUY', 'Up', 'pending', '2026-09-10 00:00:00+00')
            """
        )
        await conn.execute(
            """
            INSERT INTO positions
                (market_id, outcome, quantity, avg_price,
                 realized_pnl, unrealized_pnl, updated_at)
            VALUES
                (1, 'Up', 3, 0.5, 0, 0, '2026-09-10 00:00:00+00')
            """
        )
    finally:
        await conn.close()


async def _verify_head(db_url: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(db_url))
    try:
        # 0003: legacy NULL-identity signals are removed, and source identity
        # is mandatory going forward.
        assert await conn.fetchval("SELECT COUNT(*) FROM signals") == 0
        assert await conn.fetchval(
            """
            SELECT is_nullable = 'NO'
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'signals'
              AND column_name = 'source_trade_id'
            """
        )

        # 0004: an already-approved wallet receives a deterministic copy
        # boundary rather than becoming permanently unable to signal.
        assert await conn.fetchval(
            "SELECT approved_at IS NOT NULL FROM wallets WHERE address='0xlegacy'"
        )

        # 0005: source-token evidence + wallet-scoped paper positions exist.
        signal_asset = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='signals'
              AND column_name='asset_id'
            """
        )
        assert signal_asset == 1
        position_cols = {
            row["column_name"]
            for row in await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='positions'
                """
            )
        }
        assert {"wallet_id", "settled_at"} <= position_cols

        # Pre-PR-E position placeholders had no source-wallet identity and
        # are intentionally discarded during 0005.
        assert await conn.fetchval("SELECT COUNT(*) FROM positions") == 0

        constraints = {
            row["constraint_name"]
            for row in await conn.fetch(
                """
                SELECT constraint_name
                FROM information_schema.table_constraints
                WHERE table_schema='public' AND table_name='positions'
                """
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
        pytest.skip("real PostgreSQL migration test requires POLYCOPY_TEST_POSTGRES_URL")

    cfg = _alembic_cfg(db_url, monkeypatch)
    # CI provides a dedicated disposable database. Reset it so this test is
    # deterministic across reruns in the same job.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "0002")
    asyncio.run(_seed_pre_pr_e(db_url))
    command.upgrade(cfg, "head")
    asyncio.run(_verify_head(db_url))
    get_settings.cache_clear()
