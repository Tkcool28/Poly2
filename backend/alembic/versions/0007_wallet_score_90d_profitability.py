"""Add explicit 90-day review profitability fields to wallet_scores.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallet_scores", sa.Column("profit_factor_90d", sa.Float(), nullable=True))
    op.add_column(
        "wallet_scores", sa.Column("gross_profit_90d", sa.Numeric(24, 6), nullable=True)
    )
    op.add_column(
        "wallet_scores", sa.Column("gross_loss_90d", sa.Numeric(24, 6), nullable=True)
    )
    op.add_column(
        "wallet_scores", sa.Column("realized_pnl_90d", sa.Numeric(24, 6), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("wallet_scores", "realized_pnl_90d")
    op.drop_column("wallet_scores", "gross_loss_90d")
    op.drop_column("wallet_scores", "gross_profit_90d")
    op.drop_column("wallet_scores", "profit_factor_90d")
