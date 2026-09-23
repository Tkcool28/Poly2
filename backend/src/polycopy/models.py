"""SQLAlchemy 2.0 models — full schema for the rebuild vision.

Design rules baked in here:

* Wallet approval has ONE source of truth: ``approval_state``. Any boolean
  view is derived from it, never stored independently.
* Tradable identity is first-class: markets carry ``clob_token_ids`` (the
  condition→token mapping) and trades carry ``asset_id`` (the CLOB token
  actually traded). Human-readable outcome text is display data, not a key.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


# Wallet approval state machine: the single source of truth.
# discovered -> pending_review -> approved | rejected; approved -> disabled.
# "rejected" is HUMAN-only (set via the API): the scorer's machine verdicts
# (insufficient_history / score_rejected) live in WalletScore rows and the
# decision log, never here — so auto-scored wallets always stay rescannable.
WALLET_STATES = ("discovered", "pending_review", "approved", "rejected", "disabled")


class Base(DeclarativeBase):
    pass


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(120))
    # Single source of truth for approval. No parallel boolean.
    approval_state: Mapped[str] = mapped_column(String(20), default="discovered")
    # Copy-enabled boundary (PR #7 review): set when a human approves the
    # wallet. Only trades INGESTED at/after this moment may become signals —
    # pre-approval trades are history, never copyable.
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    trades: Mapped[list[Trade]] = relationship(back_populates="wallet")

    @property
    def is_approved(self) -> bool:
        """Derived view only — never stored."""
        return self.approval_state == "approved"


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(200))
    # {"Yes": "7132104...", "No": "5211431..."} — outcome -> clob_token_id.
    # The tradable identity on Polymarket is the token, not the outcome text.
    clob_token_ids: Mapped[dict | None] = mapped_column(JSON)
    outcomes: Mapped[list | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_outcome: Mapped[str | None] = mapped_column(String(40))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    trades: Mapped[list[Trade]] = relationship(back_populates="market")


class Trade(Base):
    """A trade observed on Polymarket by any wallet (raw ingestion record).

    ``polymarket_trade_id`` holds the composite canonical key defined in
    docs/source-identity-contract.md:
    ``data-api:{txHash}:{wallet}:{asset}:{size}:{price}:{timestamp}``
    """

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("polymarket_trade_id", name="uq_trades_polymarket_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    polymarket_trade_id: Mapped[str] = mapped_column(Text)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    # The CLOB token actually traded — first-class tradable identity.
    asset_id: Mapped[str | None] = mapped_column(String(80), index=True)
    side: Mapped[str] = mapped_column(String(4))  # BUY / SELL
    outcome: Mapped[str] = mapped_column(String(40))
    size: Mapped[float] = mapped_column(Numeric(20, 6))
    price: Mapped[float] = mapped_column(Numeric(10, 6))
    fee: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    market: Mapped[Market] = relationship(back_populates="trades")
    wallet: Mapped[Wallet] = relationship(back_populates="trades")


class Settlement(Base):
    """Record of a market resolving — feeds realized P&L for scoring."""

    __tablename__ = "settlements"
    __table_args__ = (UniqueConstraint("market_id", name="uq_settlements_market"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    winning_outcome: Mapped[str] = mapped_column(String(40))
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evidence_uri: Mapped[str | None] = mapped_column(Text)


class WalletScore(Base):
    __tablename__ = "wallet_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    window_days: Mapped[int] = mapped_column(Integer, default=90)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float)
    max_drawdown: Mapped[float | None] = mapped_column(Float)
    profit_factor: Mapped[float | None] = mapped_column(Float)
    kelly_fraction: Mapped[float | None] = mapped_column(Float)
    composite_score: Mapped[float | None] = mapped_column(Float)  # 0-100
    behavioral_tags: Mapped[list | None] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Signal(Base):
    """A source-wallet trade we may copy. Idempotent via source_trade_id.

    Timestamps follow docs/paper-execution-model.md: t0 = source trade time,
    t1 = when our ingestion saw it (detection lag = t1 - t0).
    """

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("source_trade_id", name="uq_signals_source_trade"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    # Canonical identity of the source trade (data-api:... composite key).
    source_trade_id: Mapped[str] = mapped_column(Text)
    # CLOB token actually traded by the source wallet — carried from
    # Trade.asset_id so execution never depends on Gamma outcome->token
    # metadata, which may be missing or stale (PR #7 hardening).
    asset_id: Mapped[str | None] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(4))
    outcome: Mapped[str] = mapped_column(String(40))
    source_price: Mapped[float] = mapped_column(Numeric(10, 6))
    edge: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    # pending -> executed | skipped (reason in the decision log)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    t0_traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Honest detection time = when ingestion first saw the trade
    # (Trade.ingested_at), not when a later detect_signals() query ran —
    # detection-lag evidence stays truthful across restarts/backlogs.
    t1_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalQueueEntry(Base):
    __tablename__ = "approval_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    reviewer: Mapped[str | None] = mapped_column(String(80))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperOrder(Base):
    """One simulated fill. Status: filled / partial / missed.

    Evidence fields (per docs/paper-execution-model.md) record what the
    book looked like at detection/decision time so larger sizing and
    different gates can be evaluated offline later.
    """

    __tablename__ = "paper_orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_paper_orders_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    side: Mapped[str] = mapped_column(String(4))
    size: Mapped[float] = mapped_column(Numeric(20, 6))
    price: Mapped[float] = mapped_column(Numeric(10, 6))
    status: Mapped[str] = mapped_column(String(20), default="preview")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- Realistic-fill evidence (PR-E) -----------------------------------
    t2_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Volume-weighted fill price from walking the book — NEVER the source
    # wallet's price.
    fill_price: Mapped[float | None] = mapped_column(Numeric(10, 6))
    filled_size: Mapped[float | None] = mapped_column(Numeric(20, 6))
    fee: Mapped[float | None] = mapped_column(Numeric(20, 6))
    # Raw detection-time book ({"bids": [[price, size], ...], "asks": ...}).
    book_snapshot: Mapped[dict | None] = mapped_column(JSON)
    # Why status == "missed" (e.g. "no_book_depth", "market_closed",
    # "no_token_for_outcome", "exposure_cap", "no_position_to_sell",
    # "price_zone", "wallet_not_approved"). The kill switch is NOT a miss
    # reason: it defers the signal (stays pending, no order, no book
    # request) until the switch clears.
    miss_reason: Mapped[str | None] = mapped_column(String(60))


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "wallet_id", "market_id", "outcome",
            name="uq_positions_wallet_market_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Paper inventory is scoped to the source wallet being copied. Without
    # this key, one wallet's SELL could consume another wallet's paper shares.
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(40))
    quantity: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    avg_price: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
    realized_pnl: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    unrealized_pnl: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    # Set when this position was settled at market resolution (winner $1 /
    # loser $0). Idempotency marker: settlement runs skip settled rows.
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DecisionLogEntry(Base):
    """Audit trail: every automated or human decision lands here."""

    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(40))  # e.g. "bot", "api", "human:<name>"
    action: Mapped[str] = mapped_column(String(80))
    context: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ServiceHeartbeat(Base):
    """Lightweight liveness table so the dashboard can show system health."""

    __tablename__ = "service_heartbeats"

    id: Mapped[int] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(String(40), index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
