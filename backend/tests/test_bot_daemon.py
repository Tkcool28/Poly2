"""Bot-cycle containment: degraded ingestion must not block later stages."""

from types import SimpleNamespace

import pytest

import polycopy.bot.daemon as daemon


@pytest.mark.asyncio
async def test_degraded_ingestion_still_runs_settlement_and_execution(monkeypatch):
    called = []

    async def fake_ingest(session, client):
        called.append("ingest")
        return SimpleNamespace(
            wallets={"0xbad": 0, "0xgood": 1},
            failed_wallets=1,
            quarantined_rows=0,
        )

    async def fake_settle(session, client):
        called.append("settle")
        return {"settled": 1}

    async def fake_execute(session, client):
        called.append("execute")
        return {"executed": 0}

    monkeypatch.setattr(daemon, "run_ingestion_cycle", fake_ingest)
    monkeypatch.setattr(daemon, "refresh_settlements", fake_settle)
    monkeypatch.setattr(daemon, "run_execution_cycle", fake_execute)

    stats, degraded = await daemon.run_bot_cycle(object(), object())

    assert called == ["ingest", "settle", "execute"]
    assert degraded is True
    assert stats["ingest"]["failed_wallets"] == 1
