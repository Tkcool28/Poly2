"""Persist due times and retry counts for unresolved Gamma markets.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_throttles",
        sa.Column("family", sa.String(40), primary_key=True),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("markets", sa.Column("settlement_last_checked_at", sa.DateTime(timezone=True)))
    op.add_column("markets", sa.Column("settlement_next_check_at", sa.DateTime(timezone=True)))
    op.add_column("markets", sa.Column("settlement_attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_markets_settlement_next_check_at", "markets", ["settlement_next_check_at"])


def downgrade() -> None:
    op.drop_index("ix_markets_settlement_next_check_at", table_name="markets")
    op.drop_column("markets", "settlement_attempt_count")
    op.drop_column("markets", "settlement_next_check_at")
    op.drop_column("markets", "settlement_last_checked_at")
    op.drop_table("api_throttles")
