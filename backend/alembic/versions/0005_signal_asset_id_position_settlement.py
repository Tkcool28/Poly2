"""PR #7 hardening: signal asset_id + paper-position settlement marker.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-21

* signals.asset_id — the CLOB token the source wallet actually traded,
  carried from Trade.asset_id at detection time. Execution requests the
  book for THIS token; Gamma outcome->token metadata is fallback only.
* positions.wallet_id — paper inventory belongs to the source wallet being
  copied, so one wallet can never consume another wallet's copied position.
* positions.settled_at — idempotency marker for paper settlement at
  market resolution (winning shares $1, losing shares $0).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("asset_id", sa.String(80)))
    # Backfill from the originating trade where the identity still matches.
    op.execute(
        "UPDATE signals SET asset_id = ("
        "  SELECT asset_id FROM trades"
        "  WHERE trades.polymarket_trade_id = signals.source_trade_id"
        ") WHERE source_trade_id IS NOT NULL"
    )
    # Position rows were Chunk-1 placeholders before PR-E; they had no
    # source-wallet identity and therefore cannot be safely attributed.
    # Drop any such legacy placeholders before making wallet ownership
    # mandatory. Source-wallet accounting does not use this table.
    op.add_column("positions", sa.Column("wallet_id", sa.Integer()))
    op.execute("DELETE FROM positions")
    op.alter_column("positions", "wallet_id", existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(
        "fk_positions_wallet_id_wallets",
        "positions", "wallets", ["wallet_id"], ["id"],
    )
    op.create_index("ix_positions_wallet_id", "positions", ["wallet_id"])
    op.create_unique_constraint(
        "uq_positions_wallet_market_outcome",
        "positions", ["wallet_id", "market_id", "outcome"],
    )
    op.add_column("positions", sa.Column("settled_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("positions", "settled_at")
    op.drop_constraint(
        "uq_positions_wallet_market_outcome", "positions", type_="unique"
    )
    op.drop_index("ix_positions_wallet_id", table_name="positions")
    op.drop_constraint(
        "fk_positions_wallet_id_wallets", "positions", type_="foreignkey"
    )
    op.drop_column("positions", "wallet_id")
    op.drop_column("signals", "asset_id")
