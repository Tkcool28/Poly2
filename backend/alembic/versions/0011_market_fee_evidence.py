"""Persist market-specific fee evidence on new paper fills.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper_orders", sa.Column("fee_enabled", sa.Boolean()))
    op.add_column("paper_orders", sa.Column("fee_metadata_state", sa.String(12)))
    op.add_column("paper_orders", sa.Column("fee_rate_coefficient", sa.Text()))
    op.add_column("paper_orders", sa.Column("fee_exponent", sa.Text()))
    op.add_column("paper_orders", sa.Column("fee_taker_only", sa.Boolean()))
    op.add_column("paper_orders", sa.Column("fee_liquidity_role", sa.String(12)))
    op.add_column("paper_orders", sa.Column("fee_source", sa.String(100)))
    op.add_column("paper_orders", sa.Column("fee_metadata_retrieved_at", sa.DateTime(timezone=True)))
    op.add_column("paper_orders", sa.Column("fee_calculation_shares", sa.Text()))
    op.add_column("paper_orders", sa.Column("fee_calculation_price", sa.Text()))


def downgrade() -> None:
    op.drop_column("paper_orders", "fee_calculation_price")
    op.drop_column("paper_orders", "fee_calculation_shares")
    op.drop_column("paper_orders", "fee_metadata_retrieved_at")
    op.drop_column("paper_orders", "fee_source")
    op.drop_column("paper_orders", "fee_liquidity_role")
    op.drop_column("paper_orders", "fee_taker_only")
    op.drop_column("paper_orders", "fee_exponent")
    op.drop_column("paper_orders", "fee_rate_coefficient")
    op.drop_column("paper_orders", "fee_metadata_state")
    op.drop_column("paper_orders", "fee_enabled")
