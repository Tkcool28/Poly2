"""Prospective integration tests: disabled source path and installed identity."""
import subprocess
from pathlib import Path

import pytest

from polycopy.config import Settings
from polycopy.shadow_capture import install_factory, status


def test_capture_disabled_zero_work(tmp_path):
    maker = object()
    settings = Settings(_env_file=None)
    assert install_factory(maker, settings) is maker
    assert status()['state'] == 'DISABLED'
    assert list(tmp_path.iterdir()) == []


def test_capture_missing_authority_source_remains_available(tmp_path):
    maker = object()
    settings = Settings(_env_file=None, shadow_capture_enabled=True,
                        shadow_capture_health_path=str(tmp_path / 'health.json'))
    assert install_factory(maker, settings) is maker
    assert status()['state'] == 'FAILED'
    assert status()['comparisonEligible'] is False


@pytest.mark.parametrize("path", [
    "ingestion/service.py", "ingestion/catchup.py", "scoring/service.py", "scoring/score.py",
    "execution/service.py", "execution/bookwalk.py", "execution/fees.py",
    "api/routes.py", "accounting/settlements.py", "accounting/pnl.py", "bankroll.py",
])
def test_capture_trading_business_bytes_unchanged(path):
    root = Path(__file__).resolve().parents[2]
    relative = "backend/src/polycopy/" + path
    # The frozen base is source authority; no current config or mutable data.
    original = subprocess.check_output(["git", "show", "bd61efc90a4ff8bfc17b8a31abc4a40dd655f81f:" + relative], cwd=root)
    assert (root / relative).read_bytes() == original


def test_capture_safety_flags_default_and_failure(tmp_path):
    settings = Settings(_env_file=None, shadow_capture_enabled=True,
                        shadow_capture_health_path=str(tmp_path / "health.json"))
    before = settings.model_dump()
    maker = object()
    assert install_factory(maker, settings) is maker
    assert settings.model_dump() == before
    assert settings.paper_mode and not settings.allow_live_trading and settings.order_kill_switch
    assert settings.max_exposure_global_usd == 200 and settings.max_order_size_usd == 10


def test_capture_shared_factory_installed_before_cache(monkeypatch):
    import polycopy.shadow_capture as capture
    from polycopy import db
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_sessionmaker", None)
    events = []
    engine, ordinary, observed = object(), object(), object()
    monkeypatch.setattr(db, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(db, "async_sessionmaker", lambda *args, **kwargs: ordinary)
    def install(maker, settings):
        events.append(maker)
        return observed
    monkeypatch.setattr(capture, "install_factory", install)
    assert db.get_sessionmaker() is observed
    assert db.get_sessionmaker() is observed
    assert events == [ordinary]
