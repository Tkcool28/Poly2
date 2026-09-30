"""Bankroll sweep-pending flag for partial profit withdrawals.

A profit sweep can only move cash that is actually free (not deployed in
open positions). When free cash covers only part of the profit pile, the
sweep moves what it can and sets ``sweep_pending`` so a later cycle can
finish the withdrawal without the remainder having to re-cross the
profit limit.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bankroll_accounts",
        sa.Column(
            "sweep_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("bankroll_accounts", "sweep_pending")
