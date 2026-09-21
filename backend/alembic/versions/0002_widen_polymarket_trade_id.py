"""Widen trades.polymarket_trade_id for composite canonical keys.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

The source-identity audit (docs/source-identity-contract.md) decided the
canonical trade key is a composite string like
"data-api:{txHash}:{wallet}:{asset}:{size}:{price}:{ts}", which exceeds the
original String(80).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "trades",
        "polymarket_trade_id",
        type_=sa.String(160),
        existing_type=sa.String(80),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "trades",
        "polymarket_trade_id",
        type_=sa.String(80),
        existing_type=sa.String(160),
        existing_nullable=False,
    )
