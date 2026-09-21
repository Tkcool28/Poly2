"""FastAPI application — health, status, config, and dashboard read endpoints."""

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
from polycopy.models import Market, PaperOrder, Position, Signal, Wallet, WalletScore

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


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


async def _latest_scores(db: AsyncSession, wallet_ids: list[int]) -> dict[int, WalletScore]:
    """Latest score per wallet id, in one bounded round trip."""
    if not wallet_ids:
        return {}
    rows = (
        await db.execute(
            select(WalletScore)
            .where(WalletScore.wallet_id.in_(wallet_ids))
            .order_by(WalletScore.computed_at.desc())
            .limit(2000)
        )
    ).scalars().all()
    latest: dict[int, WalletScore] = {}
    for score in rows:  # desc order: first row per wallet wins
        latest.setdefault(score.wallet_id, score)
    return latest


@app.get("/wallets")
async def list_wallets(db: AsyncSession = Depends(get_db)) -> dict:
    """All tracked wallets with their latest score (dashboard Wallets tab)."""
    result = await db.execute(select(Wallet).order_by(Wallet.id).limit(500))
    wallets = result.scalars().all()
    scores = await _latest_scores(db, [w.id for w in wallets])
    return {
        "items": [
            {
                "id": w.id,
                "address": w.address,
                "label": w.label,
                "approval_state": w.approval_state,
                "is_approved": w.is_approved,  # derived from approval_state
                "is_sample": w.is_sample,
                "approved_at": _iso(w.approved_at),
                "created_at": _iso(w.created_at),
                "composite_score": (
                    scores[w.id].composite_score if w.id in scores else None
                ),
            }
            for w in wallets
        ],
        "count": len(wallets),
    }


@app.get("/signals")
async def list_signals(db: AsyncSession = Depends(get_db)) -> dict:
    """Recent signals with market/wallet context and the copy result.

    One row per signal, newest first — the dashboard Activity tab renders
    this directly, so each item carries everything needed to explain in
    plain language what the followed wallet did and what the bot did
    about it (filled / partial / missed + why).
    """
    rows = (
        await db.execute(
            select(Signal, Market, Wallet)
            .join(Market, Signal.market_id == Market.id)
            .join(Wallet, Signal.wallet_id == Wallet.id)
            .order_by(Signal.id.desc())
            .limit(200)
        )
    ).all()
    orders = (
        await db.execute(
            select(PaperOrder)
            .where(PaperOrder.signal_id.in_([s.id for s, _, _ in rows]))
            .limit(500)
        )
    ).scalars().all() if rows else []
    order_by_signal = {o.signal_id: o for o in orders}
    items = []
    for s, market, wallet in rows:
        order = order_by_signal.get(s.id)
        items.append(
            {
                "id": s.id,
                "wallet_id": s.wallet_id,
                "wallet_address": wallet.address,
                "wallet_label": wallet.label,
                "market_id": s.market_id,
                "market_question": market.question,
                "side": s.side,
                "outcome": s.outcome,
                "source_price": float(s.source_price),
                "status": s.status,
                "t0_traded_at": _iso(s.t0_traded_at),
                "t1_detected_at": _iso(s.t1_detected_at),
                "created_at": _iso(s.created_at),
                "paper_order": (
                    {
                        "status": order.status,
                        "miss_reason": order.miss_reason,
                        "fill_price": (
                            float(order.fill_price)
                            if order.fill_price is not None else None
                        ),
                        "filled_size": (
                            float(order.filled_size)
                            if order.filled_size is not None else None
                        ),
                        "fee": float(order.fee) if order.fee is not None else None,
                        "t2_decided_at": _iso(order.t2_decided_at),
                    }
                    if order else None
                ),
            }
        )
    return {"items": items, "count": len(items)}


@app.get("/positions")
async def list_positions(db: AsyncSession = Depends(get_db)) -> dict:
    """Paper portfolio: open and settled positions with context + totals.

    Rows with quantity > 0 are open; rows with settled_at set were settled
    at market resolution (winner $1 / loser $0). Totals let the dashboard
    headline realized practice P&L without client-side math.
    """
    rows = (
        await db.execute(
            select(Position, Market, Wallet)
            .join(Market, Position.market_id == Market.id)
            .join(Wallet, Position.wallet_id == Wallet.id)
            .order_by(Position.id.desc())
            .limit(500)
        )
    ).all()
    items = [
        {
            "id": p.id,
            "wallet_id": p.wallet_id,
            "wallet_address": w.address,
            "wallet_label": w.label,
            "market_id": p.market_id,
            "market_question": m.question,
            "market_closed": m.closed,
            "outcome": p.outcome,
            "quantity": float(p.quantity),
            "avg_price": float(p.avg_price),
            "realized_pnl": float(p.realized_pnl),
            "settled_at": _iso(p.settled_at),
        }
        for p, m, w in rows
    ]
    open_items = [i for i in items if i["quantity"] > 0 and i["settled_at"] is None]
    return {
        "items": items,
        "count": len(items),
        "totals": {
            "open_count": len(open_items),
            "open_cost_usd": sum(i["quantity"] * i["avg_price"] for i in open_items),
            "realized_pnl_usd": sum(i["realized_pnl"] for i in items),
            "settled_count": len([i for i in items if i["settled_at"] is not None]),
        },
    }
