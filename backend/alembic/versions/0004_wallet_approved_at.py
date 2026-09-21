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
    # Wallets approved BEFORE this column existed would otherwise get
    # approved_at=NULL and never produce signals again (the copy-enabled
    # boundary requires approved_at IS NOT NULL). Backfill deterministically
    # from the wallet's own timestamps — updated_at approximates the last
    # transition; created_at is the floor. No manual repair transition
    # needed (PR #7 hardening).
    op.execute(
        "UPDATE wallets SET approved_at = COALESCE(updated_at, created_at) "
        "WHERE approval_state = 'approved' AND approved_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("wallets", "approved_at")
