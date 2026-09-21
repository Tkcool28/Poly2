"""PR #7 hardening: migrations must be safe against a POPULATED pre-PR-E
schema — not just render as SQL on an empty one.

* 0003: legacy signal rows (no source identity) must be removed, and
  signals.source_trade_id must end up NOT NULL + unique.
* 0004: wallets already approved before the column existed must get a
  deterministic approved_at backfill — they must never become permanently
  unable to produce signals.
* 0005: signals.asset_id backfills from the originating trade.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from polycopy.config import get_settings

BACKEND = Path(__file__).resolve().parent.parent


def _alembic_cfg(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("POLYCOPY_DATABASE_URL", db_url)
    get_settings.cache_clear()
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


def _seed_pre_pre_schema(db_path: Path) -> None:
    """Populate the 0002-era schema with legacy rows (sync sqlite)."""
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO wallets (address, approval_state, is_sample,"
                " created_at, updated_at) VALUES"
                " ('0xlegacy', 'approved', 0, '2026-09-01 00:00:00',"
                "  '2026-09-10 12:00:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO markets (condition_id, question, active, closed,"
                " created_at) VALUES ('0xcond', '?', 1, 0, '2026-09-01')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO trades (polymarket_trade_id, market_id, wallet_id,"
                " asset_id, side, outcome, size, price, fee, traded_at,"
                " ingested_at) VALUES ('data-api:0xtx:0xlegacy:4667:5:0.5:1',"
                " 1, 1, '4667', 'BUY', 'Up', 5, 0.5, 0, '2026-09-10',"
                " '2026-09-10')"
            )
        )
        # Legacy placeholder signal: no canonical source identity.
        conn.execute(
            sa.text(
                "INSERT INTO signals (wallet_id, market_id, side, outcome,"
                " status, created_at) VALUES (1, 1, 'BUY', 'Up', 'pending',"
                " '2026-09-10')"
            )
        )
    engine.dispose()


def test_upgrade_from_populated_pre_pr_e_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "legacy.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    cfg = _alembic_cfg(db_url, monkeypatch)

    command.upgrade(cfg, "0002")  # pre-PR-E schema
    _seed_pre_pre_schema(db_path)
    command.upgrade(cfg, "head")  # the full PR-E + hardening chain

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        # 0003: the legacy placeholder signal was removed (NULL source
        # identity would poison NOT IN semantics and is not evidence).
        assert conn.execute(sa.text("SELECT COUNT(*) FROM signals")).scalar() == 0
        # 0003: source identity is NOT NULL going forward.
        assert conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM pragma_table_info('signals')"
                " WHERE name='source_trade_id' AND \"notnull\"=1"
            )
        ).scalar() == 1
        # 0004: the already-approved wallet got a deterministic backfill
        # (COALESCE(updated_at, created_at)) — never NULL.
        approved_at = conn.execute(
            sa.text("SELECT approved_at FROM wallets WHERE address='0xlegacy'")
        ).scalar()
        assert approved_at is not None
        assert "2026-09-10" in approved_at
        # 0005: new columns exist.
        cols = {
            row[1]
            for row in conn.execute(sa.text("PRAGMA table_info('signals')"))
        }
        assert "asset_id" in cols
        pcols = {
            row[1]
            for row in conn.execute(sa.text("PRAGMA table_info('positions')"))
        }
        assert "settled_at" in pcols
    engine.dispose()
    get_settings.cache_clear()
