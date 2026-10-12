"""Real Alembic fresh/upgrade/downgrade on an explicitly owned native fixture."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


async def test_capture_native_migration_roundtrip():
    explicit = os.environ.get("POLY2_CAPTURE_TEST_URL")
    socket = os.environ.get("POLY2_CAPTURE_TEST_SOCKET")
    if explicit:
        assert explicit.startswith(("postgresql+asyncpg://polycopy@localhost:",
                                    "postgresql+asyncpg://polycopy@127.0.0.1:"))
        assert explicit.endswith("/polycopy_test")
        base = make_url(explicit)
        admin_url = base.set(database="postgres")
        name = "poly2_capture_migration_owned"
        url = base.set(database=name)
    else:
        if not socket:
            pytest.skip("explicit owned Unix socket or localhost CI fixture required")
        assert socket.startswith("/proc/") and socket.endswith("/root/var/run/postgresql")
        admin_url = make_url("postgresql+asyncpg://postgres@/postgres?host=" + socket)
        name = "poly2_capture_migration_owned"
        url = make_url("postgresql+asyncpg://postgres@/" + name + "?host=" + socket)
    root = Path(__file__).resolve().parents[1]
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    engine = create_async_engine(url)
    async with admin.connect() as conn:
        # Never replace a preexisting database, even within an owned fixture.
        await conn.execute(text("CREATE DATABASE " + name))
    async def alembic(*args):
        env = {**os.environ, "POLYCOPY_DATABASE_URL": str(url),
               "POLYCOPY_PAPER_MODE": "true", "POLYCOPY_ALLOW_LIVE_TRADING": "false",
               "POLYCOPY_ORDER_KILL_SWITCH": "true", "POLYCOPY_SHADOW_CAPTURE_ENABLED": "false"}
        p = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "alembic", *args],
                                    cwd=root, env=env, text=True, capture_output=True, check=False)
        assert p.returncode == 0, p.stdout + p.stderr
        print("MIGRATION", " ".join(args), "exit", p.returncode)
    try:
        await alembic("upgrade", "head")
        async with engine.begin() as conn:
            columns = (await conn.execute(text("SELECT column_name,data_type,is_nullable FROM information_schema.columns WHERE table_name='comparison_transaction_witness' ORDER BY ordinal_position"))).all()
            assert columns == [("transaction_id", "text", "NO"), ("binding_sha256", "text", "NO"),
                               ("payload", "text", "NO"), ("payload_sha256", "text", "NO")]
            await conn.execute(text("INSERT INTO comparison_transaction_witness VALUES('retained','binding','payload','digest')"))
        await alembic("downgrade", "0013")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0013"
            assert (await conn.execute(text("SELECT * FROM comparison_transaction_witness"))).one() == ("retained", "binding", "payload", "digest")
        await alembic("upgrade", "head")
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0014"
            assert await conn.scalar(text("SELECT count(*) FROM comparison_transaction_witness")) == 1
            # Now prove a legacy 0013 schema genuinely missing the table upgrades.
            await conn.execute(text("DROP TABLE comparison_transaction_witness"))
        await alembic("stamp", "0013")
        await alembic("upgrade", "head")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM comparison_transaction_witness")) == 0
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text("DROP DATABASE " + name))
        await admin.dispose()
