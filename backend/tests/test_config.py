"""Config fail-closed validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from polycopy.config import Settings


def _settings(**overrides) -> Settings:
    base = {"environment": "test", "_env_file": None}
    return Settings(**(base | overrides))


def test_defaults_are_safe():
    s = _settings()
    assert s.paper_mode is True
    assert s.allow_live_trading is False
    assert s.order_kill_switch is True  # kill switch defaults ON


def test_private_key_rejected_without_live_trading():
    with pytest.raises(ValidationError, match="ALLOW_LIVE_TRADING"):
        _settings(polymarket_private_key="0xdeadbeef")


def test_live_trading_with_paper_mode_is_ambiguous_and_rejected():
    with pytest.raises(ValidationError, match="ambiguous"):
        _settings(allow_live_trading=True, paper_mode=True)


def test_live_capable_boot_with_kill_switch_on_is_allowed():
    """The intended staging posture: live-capable, execution blocked."""
    s = _settings(
        allow_live_trading=True,
        paper_mode=False,
        order_kill_switch=True,
        polymarket_private_key="0xdeadbeef",
    )
    assert s.allow_live_trading is True
    assert s.order_kill_switch is True


def test_armed_live_config_is_allowed():
    """Fully armed live mode (kill switch cleared) is a legal explicit state."""
    s = _settings(
        allow_live_trading=True,
        paper_mode=False,
        order_kill_switch=False,
        polymarket_private_key="0xdeadbeef",
    )
    assert s.allow_live_trading is True
    assert s.order_kill_switch is False


def test_invalid_environment_rejected():
    with pytest.raises(ValidationError, match="environment"):
        _settings(environment="yolo")


def test_public_dict_strips_secrets():
    s = _settings(
        allow_live_trading=True,
        paper_mode=False,
        order_kill_switch=True,
        polymarket_private_key="0xdeadbeef",
    )
    public = s.public_dict()
    assert "0xdeadbeef" not in str(public)
    # credentials stripped from URL — only host/db shown
    assert public["database_url"] == s.database_url.split("@")[-1]
    assert ":" not in public["database_url"].split("/")[-1]
