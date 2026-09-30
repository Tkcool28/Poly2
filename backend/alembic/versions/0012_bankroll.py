"""Paper bankroll: account state + append-only ledger.

The paper bot behaves like a live-money bot with a fixed stake: it knows
its cash balance, refuses orders that don't fit, sweeps accumulated
profit out at a configurable limit (simulating withdrawal to a real
account), and stops opening new positions at a stop-loss floor. There is
NO deposit/refill path anywhere — a drawdown is a drawdown — so whatever
happens on paper is exactly what would happen live.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bankroll_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("starting_bankroll_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("realized_pnl_total", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("withdrawn_total", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("profit_limit_usd", sa.Numeric(20, 6), nullable=True),
        sa.Column("stop_loss_floor_usd", sa.Numeric(20, 6), nullable=True),
        sa.Column(
            "sweep_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "bankroll_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_type", sa.String(24), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("balance_after", sa.Numeric(20, 6), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bankroll_ledger_entry_type", "bankroll_ledger", ["entry_type"])


def downgrade() -> None:
    op.drop_index("ix_bankroll_ledger_entry_type", table_name="bankroll_ledger")
    op.drop_table("bankroll_ledger")
    op.drop_table("bankroll_accounts")
