"""Bot-cycle containment: degraded ingestion must not block later stages."""

from types import SimpleNamespace

import pytest

from polycopy.bot import daemon


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

    async def fake_settle(session, client, **kwargs):
        called.append("settle")
        assert kwargs["max_markets"] > 0
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


@pytest.mark.asyncio
async def test_candidate_scoring_runs_once_per_cadence_without_approval(monkeypatch):
    calls = []

    async def fake_score(_session, _client):
        calls.append("score")
        return {"0xcandidate": "pending_review"}

    monkeypatch.setattr(daemon, "score_all_wallets", fake_score)
    monkeypatch.setattr(daemon, "get_settings", lambda: SimpleNamespace(
        candidate_scoring_interval_seconds=3600,
    ))
    next_at, verdicts = await daemon.score_candidates_if_due(
        object(), object(), 0, clock=100,
    )
    assert next_at == 3700
    assert verdicts == {"0xcandidate": "pending_review"}
    assert await daemon.score_candidates_if_due(
        object(), object(), next_at, clock=200,
    ) == (next_at, None)
    assert calls == ["score"]
