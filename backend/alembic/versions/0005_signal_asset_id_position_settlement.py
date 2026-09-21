"""PR #7 hardening: signal asset_id + paper-position settlement marker.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-21

* signals.asset_id — the CLOB token the source wallet actually traded,
  carried from Trade.asset_id at detection time. Execution requests the
  book for THIS token; Gamma outcome->token metadata is fallback only.
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
    op.add_column("positions", sa.Column("settled_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("positions", "settled_at")
    op.drop_column("signals", "asset_id")
