"""API smoke tests — no database or redis required."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")

from fastapi.testclient import TestClient

from polycopy.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "polycopy-api"


def test_system_status_shows_safe_defaults(client: TestClient):
    resp = client.get("/system/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paper_mode"] is True
    assert body["allow_live_trading"] is False
    assert body["order_kill_switch"] is True
    assert "limits" in body


def test_config_endpoint_excludes_secrets(client: TestClient):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "0x" not in resp.text
    assert "polycopy:polycopy@" not in resp.text


def test_bot_daemon_importable_and_is_coroutine():
    """The Chunk 1 bot skeleton must at least import and define run()."""
    import inspect

    from polycopy.bot import daemon

    assert inspect.iscoroutinefunction(daemon.run)
