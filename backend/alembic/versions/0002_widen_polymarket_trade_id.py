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

from alembic import context, op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _alter_id_column(type_, existing_type) -> None:
    """Batch mode only where needed (live SQLite); plain ALTER elsewhere —
    offline --sql rendering has no live DB to reflect a batch from."""
    if context.is_offline_mode() or op.get_bind().dialect.name != "sqlite":
        op.alter_column(
            "trades",
            "polymarket_trade_id",
            type_=type_,
            existing_type=existing_type,
            existing_nullable=False,
        )
        return
    # Live SQLite: the only way ALTER COLUMN type works (PR #7 hardening —
    # populated-schema upgrades are regression-tested on SQLite).
    with op.batch_alter_table("trades") as batch_op:
        batch_op.alter_column(
            "polymarket_trade_id",
            type_=type_,
            existing_type=existing_type,
            existing_nullable=False,
        )


def upgrade() -> None:
    _alter_id_column(sa.String(160), sa.String(80))


def downgrade() -> None:
    _alter_id_column(sa.String(80), sa.String(160))
