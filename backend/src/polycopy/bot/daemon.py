"""Tailing bot daemon — ingestion → settlement → detection → paper execution.

Process/cycle-attempt liveness is recorded separately from the last clean
full cycle so failures are visible without making a live process look dead.
"""

from __future__ import annotations

import asyncio
import signal
import time
from datetime import UTC, datetime
from typing import Any

from polycopy.accounting.settlements import refresh_settlements
from polycopy.config import get_settings
from polycopy.db import get_sessionmaker
from polycopy.execution.service import run_execution_cycle
from polycopy.failure_throttle import FailureLogThrottle
from polycopy.ingestion.client import PolymarketClient
from polycopy.ingestion.service import run_ingestion_cycle
from polycopy.logging_config import configure_logging, get_logger
from polycopy.models import ServiceHeartbeat
from polycopy.scoring.service import score_all_wallets

_cycle_failure_throttle = FailureLogThrottle(
    trace_interval_seconds=300.0,
    summary_every=20,
)


async def run_bot_cycle(session, client: PolymarketClient) -> tuple[dict[str, Any], bool]:
    ingest = await run_ingestion_cycle(session, client)
    settle = await refresh_settlements(
        session, client, max_markets=get_settings().settlement_max_checks_per_cycle,
    )
    exec_stats = await run_execution_cycle(session, client)
    degraded = (ingest.failed_wallets > 0 or ingest.quarantined_rows > 0
                or settle.get("errors", 0) > 0)
    return (
        {
            "ingest": {
                "wallets": ingest.wallets,
                "failed_wallets": ingest.failed_wallets,
                "quarantined_rows": ingest.quarantined_rows,
            },
            "settlements": settle,
            "execution": exec_stats,
        },
        degraded,
    )


async def _write_heartbeat(maker, service: str) -> None:
    async with maker() as session:
        session.add(ServiceHeartbeat(service=service, seen_at=datetime.now(UTC)))
        await session.commit()


async def _write_heartbeat_safely(maker, service: str, logger) -> None:
    try:
        await _write_heartbeat(maker, service)
    except Exception as exc:
        decision = _cycle_failure_throttle.record(
            ("heartbeat", service, type(exc).__name__, str(exc))
        )
        if decision.full_trace:
            logger.exception(
                "bot_heartbeat_write_failed",
                service=service,
                error_type=type(exc).__name__,
                error=str(exc),
                repeat_count=decision.repeat_count,
            )
        elif decision.emit_summary:
            logger.warning(
                "bot_heartbeat_write_failed_repeated",
                service=service,
                error_type=type(exc).__name__,
                error=str(exc),
                repeat_count=decision.repeat_count,
            )


async def score_candidates_if_due(
    session, client, next_at: float, *, clock: float,
) -> tuple[float, dict[str, str] | None]:
    """Own candidate scoring at an hourly cadence, independently of ingestion."""
    if clock < next_at:
        return next_at, None
    verdicts = await score_all_wallets(session, client)
    return clock + max(60, get_settings().candidate_scoring_interval_seconds), verdicts


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
    next_scoring_at = 0.0  # score soon after startup, then at the configured cadence
    async with PolymarketClient() as client:
        while not stop.is_set():
            await _write_heartbeat_safely(maker, "bot_alive", logger)
            try:
                async with maker() as session:
                    stats, degraded = await run_bot_cycle(session, client)
                async with maker() as session:
                    next_scoring_at, verdicts = await score_candidates_if_due(
                        session, client, next_scoring_at, clock=time.monotonic(),
                    )
                if verdicts is not None:
                    logger.info("automatic_candidate_scoring", verdicts=verdicts)

                if degraded:
                    await _write_heartbeat_safely(maker, "bot_failure", logger)
                    signature = (
                        "ingestion_degraded",
                        stats["ingest"]["failed_wallets"],
                        stats["ingest"]["quarantined_rows"],
                    )
                    decision = _cycle_failure_throttle.record(signature)
                    if decision.full_trace or decision.emit_summary:
                        logger.warning(
                            "bot_cycle_degraded",
                            **stats["ingest"],
                            repeat_count=decision.repeat_count,
                        )
                else:
                    await _write_heartbeat_safely(maker, "bot_success", logger)
                    _cycle_failure_throttle.clear_prefix("cycle_exception")
                logger.info("bot_cycle", degraded=degraded, **stats)
            except Exception as exc:
                await _write_heartbeat_safely(maker, "bot_failure", logger)
                signature = ("cycle_exception", type(exc).__name__, str(exc))
                decision = _cycle_failure_throttle.record(signature)
                if decision.full_trace:
                    logger.exception(
                        "bot_cycle_failed",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        repeat_count=decision.repeat_count,
                    )
                elif decision.emit_summary:
                    logger.warning(
                        "bot_cycle_failed_repeated",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        repeat_count=decision.repeat_count,
                    )

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
