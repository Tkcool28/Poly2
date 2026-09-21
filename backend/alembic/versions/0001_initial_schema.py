"""Initial schema — explicit frozen migration.

Revision ID: 0001
Revises:
Create Date: 2026-09-19

This migration is intentionally explicit (no metadata.create_all) so the
schema history is stable and reviewable. Future changes use incremental
revisions.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("approval_state", sa.String(20), nullable=False, server_default="discovered"),
        sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("address", name="uq_wallets_address"),
    )
    op.create_index("ix_wallets_address", "wallets", ["address"])

    op.create_table(
        "markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("condition_id", sa.String(80), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(200), nullable=True),
        sa.Column("clob_token_ids", sa.JSON(), nullable=True),
        sa.Column("outcomes", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_outcome", sa.String(40), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("condition_id", name="uq_markets_condition_id"),
    )
    op.create_index("ix_markets_condition_id", "markets", ["condition_id"])

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("polymarket_trade_id", sa.String(80), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("wallet_id", sa.Integer(), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("asset_id", sa.String(80), nullable=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("size", sa.Numeric(20, 6), nullable=False),
        sa.Column("price", sa.Numeric(10, 6), nullable=False),
        sa.Column("fee", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("traded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("polymarket_trade_id", name="uq_trades_polymarket_id"),
    )
    op.create_index("ix_trades_market_id", "trades", ["market_id"])
    op.create_index("ix_trades_wallet_id", "trades", ["wallet_id"])
    op.create_index("ix_trades_asset_id", "trades", ["asset_id"])
    op.create_index("ix_trades_traded_at", "trades", ["traded_at"])

    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("winning_outcome", sa.String(40), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_uri", sa.Text(), nullable=True),
        sa.UniqueConstraint("market_id", name="uq_settlements_market"),
    )

    op.create_table(
        "wallet_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wallet_id", sa.Integer(), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("sharpe_ratio", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=True),
        sa.Column("profit_factor", sa.Float(), nullable=True),
        sa.Column("kelly_fraction", sa.Float(), nullable=True),
        sa.Column("composite_score", sa.Float(), nullable=True),
        sa.Column("behavioral_tags", sa.JSON(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wallet_scores_wallet_id", "wallet_scores", ["wallet_id"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wallet_id", sa.Integer(), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("edge", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_signals_wallet_id", "signals", ["wallet_id"])
    op.create_index("ix_signals_market_id", "signals", ["market_id"])

    op.create_table(
        "approval_queue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wallet_id", sa.Integer(), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reviewer", sa.String(80), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_approval_queue_wallet_id", "approval_queue", ["wallet_id"])

    op.create_table(
        "paper_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("wallet_id", sa.Integer(), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("size", sa.Numeric(20, 6), nullable=False),
        sa.Column("price", sa.Numeric(10, 6), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="preview"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_paper_orders_idempotency"),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("avg_price", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_positions_market_id", "positions", ["market_id"])

    op.create_table(
        "decision_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(40), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "service_heartbeats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service", sa.String(40), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_service_heartbeats_service", "service_heartbeats", ["service"])


def downgrade() -> None:
    for table in (
        "service_heartbeats",
        "decision_log",
        "positions",
        "paper_orders",
        "approval_queue",
        "signals",
        "wallet_scores",
        "settlements",
        "trades",
        "markets",
        "wallets",
    ):
        op.drop_table(table)
