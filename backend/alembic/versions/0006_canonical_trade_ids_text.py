"""Use unbounded text for canonical source-trade identity.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23

The Data API canonical key concatenates transaction hash, proxy wallet,
asset/token ID, normalized size/price, and timestamp. Upstream does not
publish a safe aggregate maximum and real-format keys exceed VARCHAR(160).
The same canonical identity flows into signals.source_trade_id, so both
surfaces must stay aligned.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "trades",
        "polymarket_trade_id",
        type_=sa.Text(),
        existing_type=sa.String(160),
        existing_nullable=False,
    )
    op.alter_column(
        "signals",
        "source_trade_id",
        type_=sa.Text(),
        existing_type=sa.String(160),
        existing_nullable=False,
    )


def downgrade() -> None:
    # PostgreSQL will fail rather than truncate if rows longer than 160
    # exist. That is intentional: downgrades must never destroy identity.
    op.alter_column(
        "signals",
        "source_trade_id",
        type_=sa.String(160),
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "trades",
        "polymarket_trade_id",
        type_=sa.String(160),
        existing_type=sa.Text(),
        existing_nullable=False,
    )
