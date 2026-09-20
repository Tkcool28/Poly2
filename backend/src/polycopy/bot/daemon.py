"""Tailing bot daemon — ingestion → signal detection → paper execution.

One cycle per ``ingestion_poll_interval_seconds``:

1. ingest a bounded batch of trades for every tracked wallet;
2. turn new approved-wallet trades into signals (idempotent);
3. simulate each pending signal against the detection-time order book.

Everything is paper. Live trading is impossible here: the Settings
validator refuses a private key unless POLYCOPY_ALLOW_LIVE_TRADING=true,
and the kill switch blocks even paper order creation when on.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime

from polycopy.config import get_settings
from polycopy.db import get_sessionmaker
from polycopy.execution.service import run_execution_cycle
from polycopy.ingestion.client import PolymarketClient
from polycopy.ingestion.service import run_ingestion_cycle
from polycopy.logging_config import configure_logging, get_logger
from polycopy.models import ServiceHeartbeat


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger("polycopy.bot")

    logger.info(
        "bot_starting",
        mode="paper",
        paper_mode=settings.paper_mode,
        kill_switch=settings.order_kill_switch,
        max_order_size_usd=settings.max_order_size_usd,
        poll_seconds=settings.ingestion_poll_interval_seconds,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    maker = get_sessionmaker()
    async with PolymarketClient() as client:
        while not stop.is_set():
            try:
                async with maker() as session:
                    ingest = await run_ingestion_cycle(session, client)
                    exec_stats = await run_execution_cycle(session, client)
                    session.add(
                        ServiceHeartbeat(service="bot", seen_at=datetime.now(UTC))
                    )
                    await session.commit()
                logger.info("bot_cycle", ingest=ingest, execution=exec_stats)
            except Exception:
                logger.exception("bot_cycle_failed")

            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.ingestion_poll_interval_seconds
                )
            except TimeoutError:
                pass

    logger.info("bot_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
