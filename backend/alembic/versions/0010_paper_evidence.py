"""Store paper execution request and book-walk evidence.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper_orders", sa.Column("requested_size_usd", sa.Numeric(20, 6)))
    op.add_column("paper_orders", sa.Column("book_depth_shares", sa.Numeric(20, 6)))
    op.add_column("paper_orders", sa.Column("levels_consumed", sa.Integer()))
    op.add_column("paper_orders", sa.Column("realized_pnl_delta", sa.Numeric(20, 6)))
    op.add_column("positions", sa.Column("settlement_realized_pnl", sa.Numeric(20, 6)))


def downgrade() -> None:
    op.drop_column("positions", "settlement_realized_pnl")
    op.drop_column("paper_orders", "realized_pnl_delta")
    op.drop_column("paper_orders", "levels_consumed")
    op.drop_column("paper_orders", "book_depth_shares")
    op.drop_column("paper_orders", "requested_size_usd")
