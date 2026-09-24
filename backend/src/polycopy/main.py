"""FastAPI application — health, status, config, and dashboard read endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from polycopy.api.routes import router as api_router
from polycopy.config import Settings, get_settings
from polycopy.db import dispose_engine, get_db
from polycopy.logging_config import configure_logging, get_logger
from polycopy.models import (
    Market,
    PaperOrder,
    Position,
    ServiceHeartbeat,
    Signal,
    Wallet,
    WalletScore,
)
from polycopy.paper_evidence import wallet_paper_evidence

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

    heartbeat_times: dict[str, str | None] = {
        "process_seen_at": None,
        "last_success_at": None,
        "last_failure_at": None,
    }
    try:
        rows = (
            await db.execute(
                select(ServiceHeartbeat)
                .where(
                    ServiceHeartbeat.service.in_(
                        ["bot_alive", "bot_success", "bot_failure", "bot"]
                    )
                )
                .order_by(ServiceHeartbeat.seen_at.desc(), ServiceHeartbeat.id.desc())
                .limit(50)
            )
        ).scalars().all()
        latest: dict[str, ServiceHeartbeat] = {}
        for row in rows:
            latest.setdefault(row.service, row)

        alive = latest.get("bot_alive") or latest.get("bot")
        success = latest.get("bot_success") or latest.get("bot")
        failure = latest.get("bot_failure")
        stale_after = max(60.0, settings.ingestion_poll_interval_seconds * 3)
        now = datetime.now(UTC)

        def aware(dt):
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

        if alive is None:
            checks["bot"] = "missing"
        else:
            alive_at = aware(alive.seen_at)
            heartbeat_times["process_seen_at"] = alive_at.isoformat()
            checks["bot"] = (
                "ok" if (now - alive_at).total_seconds() <= stale_after else "stale"
            )

        success_at = aware(success.seen_at) if success is not None else None
        failure_at = aware(failure.seen_at) if failure is not None else None
        heartbeat_times["last_success_at"] = (
            success_at.isoformat() if success_at is not None else None
        )
        heartbeat_times["last_failure_at"] = (
            failure_at.isoformat() if failure_at is not None else None
        )

        if success_at is None:
            checks["bot_cycle"] = "failing" if failure_at is not None else "missing"
        elif failure_at is not None and failure_at > success_at:
            checks["bot_cycle"] = "failing"
        elif (now - success_at).total_seconds() > stale_after:
            checks["bot_cycle"] = "stale"
        else:
            checks["bot_cycle"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["bot"] = f"error: {type(exc).__name__}"
        checks["bot_cycle"] = f"error: {type(exc).__name__}"

    ok = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if ok else "degraded",
        "checks": checks,
        "heartbeat": heartbeat_times,
    }


@app.get("/system/status")
async def system_status(settings: Settings = Depends(get_settings)) -> dict:
    """What the dashboard banner bar reads. No secrets."""
    return {
        "environment": settings.environment,
        "paper_mode": settings.paper_mode,
        "allow_live_trading": settings.allow_live_trading,
        "order_kill_switch": settings.order_kill_switch,
        "review_delay_seconds": settings.review_delay_seconds,
        "max_signal_execution_age_seconds": settings.max_signal_execution_age_seconds,
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


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _latest_scores(db: AsyncSession, wallet_ids: list[int]) -> dict[int, WalletScore]:
    """Latest score per requested wallet, without truncating score history."""
    if not wallet_ids:
        return {}
    ranked = (
        select(
            WalletScore.id.label("score_id"),
            func.row_number()
            .over(
                partition_by=WalletScore.wallet_id,
                order_by=(WalletScore.computed_at.desc(), WalletScore.id.desc()),
            )
            .label("rn"),
        )
        .where(WalletScore.wallet_id.in_(wallet_ids))
        .subquery()
    )
    rows = (
        await db.execute(
            select(WalletScore)
            .join(ranked, WalletScore.id == ranked.c.score_id)
            .where(ranked.c.rn == 1)
        )
    ).scalars().all()
    return {score.wallet_id: score for score in rows}


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
                "score_verdict": (
                    scores[w.id].behavioral_tags[0].get("verdict")
                    if (
                        w.id in scores
                        and scores[w.id].behavioral_tags
                        and isinstance(scores[w.id].behavioral_tags[0], dict)
                    )
                    else None
                ),
                "score_computed_at": (
                    _iso(scores[w.id].computed_at) if w.id in scores else None
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
                "source_trade_id": s.source_trade_id,
                "eligible_at": _iso(_aware(s.t1_detected_at)
                                    + timedelta(seconds=get_settings().review_delay_seconds)),
                "detection_lag_seconds": round((
                    _aware(s.t1_detected_at) - _aware(s.t0_traded_at)
                ).total_seconds(), 3),
                "kill_switch_deferrals": s.kill_switch_deferrals,
                "status": s.status,
                "t0_traded_at": _iso(s.t0_traded_at),
                "t1_detected_at": _iso(s.t1_detected_at),
                "source_ingested_at": _iso(s.t1_detected_at),
                "signal_age_seconds": (
                    round(((_aware(order.t2_decided_at) if order and order.t2_decided_at
                            else datetime.now(UTC)) - _aware(s.t0_traded_at)).total_seconds(), 3)
                    if s.t0_traded_at else None
                ),
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
                        "requested_size_usd": float(order.requested_size_usd) if order.requested_size_usd is not None else None,
                        "book_depth_shares": float(order.book_depth_shares) if order.book_depth_shares is not None else None,
                        "levels_consumed": order.levels_consumed,
                        "book_snapshot": order.book_snapshot,
                        "top_of_book_price": (
                            min(float(p) for p, _ in order.book_snapshot.get("asks", []))
                            if order.book_snapshot and s.side == "BUY" and order.book_snapshot.get("asks")
                            else max(float(p) for p, _ in order.book_snapshot.get("bids", []))
                            if order.book_snapshot and s.side == "SELL" and order.book_snapshot.get("bids")
                            else None
                        ),
                        "adverse_slippage": (
                            round((float(order.fill_price) - float(s.source_price))
                                  * (1 if s.side == "BUY" else -1), 6)
                            if order.fill_price is not None else None
                        ),
                        "t2_decided_at": _iso(order.t2_decided_at),
                        "executed_at": _iso(order.filled_at),
                        "missed_at": _iso(order.t2_decided_at) if order.status == "missed" else None,
                    }
                    if order else None
                ),
            }
        )
    return {"items": items, "count": len(items)}


@app.get("/paper/evidence")
async def paper_evidence(db: AsyncSession = Depends(get_db)) -> dict:
    """Per-wallet lifetime copy evidence; DB is the system of record."""
    wallets = (await db.execute(
        select(Wallet).where(Wallet.approved_at.is_not(None))
        .order_by(Wallet.id).limit(100)
    )).scalars().all()
    items = [await wallet_paper_evidence(db, wallet) for wallet in wallets]
    return {"items": items, "count": len(items), "truncated": len(items) == 100}


@app.get("/paper/backlog")
async def paper_backlog(db: AsyncSession = Depends(get_db)) -> dict:
    """Read-only pending/stale inventory before any controlled paper trial."""
    settings = get_settings()
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.max_signal_execution_age_seconds)
    pending = int(await db.scalar(
        select(func.count(Signal.id)).where(Signal.status == "pending")
    ) or 0)
    stale = int(await db.scalar(
        select(func.count(Signal.id)).where(
            Signal.status == "pending", Signal.t0_traded_at < stale_before,
        )
    ) or 0)
    return {"pending": pending, "stale_pending": stale,
            "kill_switch_enabled": settings.order_kill_switch,
            "max_signal_execution_age_seconds": settings.max_signal_execution_age_seconds}


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
            "updated_at": _iso(p.updated_at),
        }
        for p, m, w in rows
    ]
    open_condition = (Position.quantity > 0) & Position.settled_at.is_(None)
    totals_row = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(case((open_condition, 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (open_condition, Position.quantity * Position.avg_price),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(Position.realized_pnl), 0),
                func.coalesce(
                    func.sum(case((Position.settled_at.is_not(None), 1), else_=0)), 0
                ),
            )
        )
    ).one()
    return {
        "items": items,
        "count": len(items),
        "totals": {
            # Totals intentionally aggregate the full position table; the
            # display list remains bounded to the newest 500 rows.
            "open_count": int(totals_row[0]),
            "open_cost_usd": float(totals_row[1]),
            "realized_pnl_usd": float(totals_row[2]),
            "settled_count": int(totals_row[3]),
        },
    }
