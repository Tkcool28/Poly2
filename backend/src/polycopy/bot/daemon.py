"""Tailing bot daemon — CHUNK 1 SKELETON ONLY.

This process intentionally does nothing except prove that the packaging,
config loading, and service supervision work end-to-end. Ingestion, signal
detection, and paper execution all land in Chunk 2 behind this same
entrypoint.

Even in Chunk 2+, this daemon can never trade live unless
POLYCOPY_ALLOW_LIVE_TRADING=true — and the Settings validator makes unsafe
combinations fail at startup.
"""

from __future__ import annotations

import asyncio
import signal

from polycopy.config import get_settings
from polycopy.logging_config import configure_logging, get_logger


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger("polycopy.bot")

    logger.info(
        "bot_starting",
        mode="skeleton",
        paper_mode=settings.paper_mode,
        kill_switch=settings.order_kill_switch,
        ingestion="disabled",
        execution="disabled",
        message="Chunk 1 scaffolding — no ingestion, no trading. Idling safely.",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            logger.info("bot_heartbeat", status="idle", execution="disabled")

    logger.info("bot_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
