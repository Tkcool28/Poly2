"""PR #7 review fix: wallets.approved_at — the copy-enabled boundary.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-21

detect_signals must never backfill historical trades into copy signals
after a wallet is approved. approved_at is set by the human approve
transition; only trades ingested at/after it can generate signals.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallets", sa.Column("approved_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("wallets", "approved_at")
