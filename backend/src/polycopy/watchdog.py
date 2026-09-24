"""Independent, read-only bot success watchdog (separate container/process)."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from polycopy.db import get_sessionmaker
from polycopy.logging_config import configure_logging, get_logger
from polycopy.models import ServiceHeartbeat

logger = get_logger("polycopy.watchdog")
SUCCESS_STALE_SECONDS = 600


def heartbeat_status(rows: list[ServiceHeartbeat], now: datetime) -> str:
    """Process liveness cannot substitute for a recent successful cycle."""
    latest: dict[str, datetime] = {}
    for row in rows:
        stamp = row.seen_at if row.seen_at.tzinfo else row.seen_at.replace(tzinfo=UTC)
        latest[row.service] = max(latest.get(row.service, stamp), stamp)
    alive = latest.get("bot_alive")
    success = latest.get("bot_success")
    failure = latest.get("bot_failure")
    if alive is None or (now - alive).total_seconds() > SUCCESS_STALE_SECONDS:
        return "bot_process_missing_or_stale"
    if success is None or (now - success).total_seconds() > SUCCESS_STALE_SECONDS:
        return "bot_success_stale"
    if failure is not None and failure > success:
        return "bot_cycle_failing"
    return "ok"


async def check_once() -> str:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(
            select(ServiceHeartbeat).where(ServiceHeartbeat.service.in_(
                ["bot_alive", "bot_success", "bot_failure"],
            )).order_by(ServiceHeartbeat.id.desc()).limit(200)
        )).scalars().all()
    return heartbeat_status(rows, datetime.now(UTC))


async def run() -> None:
    previous = None
    ticks_since_log = 0
    while True:
        try:
            status = await check_once()
        except Exception:
            logger.exception("watchdog_db_error")
            status = "watchdog_db_error"
        if status != previous or (status != "ok" and ticks_since_log >= 5):
            (logger.info if status == "ok" else logger.error)(
                "bot_success_watchdog", status=status,
                stale_after_seconds=SUCCESS_STALE_SECONDS,
            )
            ticks_since_log = 0
        previous = status
        ticks_since_log += 1
        await asyncio.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-once", action="store_true")
    args = parser.parse_args()
    configure_logging("INFO")
    if args.check_once:
        raise SystemExit(0 if asyncio.run(check_once()) == "ok" else 2)
    asyncio.run(run())


if __name__ == "__main__":
    main()
