"""FastAPI application — Chunk 1: health, status, config, empty read endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.api.routes import router as api_router
from polycopy.config import Settings, get_settings
from polycopy.db import dispose_engine, get_db
from polycopy.logging_config import configure_logging, get_logger
from polycopy.models import Signal, Wallet

logger = get_logger("polycopy.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "api_starting",
        environment=settings.environment,
        paper_mode=settings.paper_mode,
        kill_switch=settings.order_kill_switch,
    )
    yield
    await dispose_engine()
    logger.info("api_stopped")


app = FastAPI(
    title="Polycopy API",
    version="0.1.0",
    description="Smart-wallet discovery and copy-trading for Polymarket. Paper-first, fail-closed.",
    lifespan=lifespan,
)

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://localhost:5173", "http://127.0.0.1"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    """Liveness: process is up. No external dependencies touched."""
    return {"status": "ok", "service": "polycopy-api", "version": "0.1.0"}


@app.get("/health/deps")
async def health_deps(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Readiness: checks Postgres and Redis connectivity."""
    checks: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 — health endpoint must not raise
        checks["postgres"] = f"error: {type(exc).__name__}"

    try:
        client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await client.ping()
        await client.aclose()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"

    ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if ok else "degraded", "checks": checks}


@app.get("/system/status")
async def system_status(settings: Settings = Depends(get_settings)) -> dict:
    """What the dashboard banner bar reads. No secrets."""
    return {
        "environment": settings.environment,
        "paper_mode": settings.paper_mode,
        "allow_live_trading": settings.allow_live_trading,
        "order_kill_switch": settings.order_kill_switch,
        "review_delay_seconds": settings.review_delay_seconds,
        "limits": {
            "max_order_size_usd": settings.max_order_size_usd,
            "max_exposure_per_market_usd": settings.max_exposure_per_market_usd,
            "max_exposure_global_usd": settings.max_exposure_global_usd,
        },
    }


@app.get("/config")
async def config_view(settings: Settings = Depends(get_settings)) -> dict:
    """Full config view with secrets stripped."""
    return settings.public_dict()


@app.get("/wallets")
async def list_wallets(db: AsyncSession = Depends(get_db)) -> dict:
    """All tracked wallets. Empty until Chunk 2 ingestion lands."""
    result = await db.execute(select(Wallet).order_by(Wallet.id).limit(500))
    wallets = result.scalars().all()
    return {
        "items": [
            {
                "id": w.id,
                "address": w.address,
                "label": w.label,
                "approval_state": w.approval_state,
                "is_approved": w.is_approved,  # derived from approval_state
                "is_sample": w.is_sample,
            }
            for w in wallets
        ],
        "count": len(wallets),
    }


@app.get("/signals")
async def list_signals(db: AsyncSession = Depends(get_db)) -> dict:
    """All signals. Empty until the scoring engine lands."""
    result = await db.execute(select(Signal).order_by(Signal.id.desc()).limit(500))
    signals = result.scalars().all()
    return {
        "items": [
            {
                "id": s.id,
                "wallet_id": s.wallet_id,
                "market_id": s.market_id,
                "side": s.side,
                "outcome": s.outcome,
                "edge": s.edge,
                "confidence": s.confidence,
                "status": s.status,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in signals
        ],
        "count": len(signals),
    }
