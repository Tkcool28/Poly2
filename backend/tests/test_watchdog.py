"""A live container cannot mask stopped successful bot cycles."""

from datetime import UTC, datetime, timedelta

from polycopy.models import ServiceHeartbeat
from polycopy.watchdog import heartbeat_status


def test_watchdog_distinguishes_alive_process_from_stale_success():
    now = datetime(2026, 9, 24, tzinfo=UTC)
    rows = [
        ServiceHeartbeat(service="bot_alive", seen_at=now - timedelta(seconds=5)),
        ServiceHeartbeat(service="bot_success", seen_at=now - timedelta(minutes=20)),
    ]
    assert heartbeat_status(rows, now) == "bot_success_stale"
    rows[1].seen_at = now - timedelta(seconds=10)
    assert heartbeat_status(rows, now) == "ok"
    rows.append(ServiceHeartbeat(service="bot_failure", seen_at=now))
    assert heartbeat_status(rows, now) == "bot_cycle_failing"
