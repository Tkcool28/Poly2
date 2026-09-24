"""Persist kill-switch deferral counts for paper evidence.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signals", sa.Column(
        "kill_switch_deferrals", sa.Integer(), nullable=False, server_default="0",
    ))


def downgrade() -> None:
    op.drop_column("signals", "kill_switch_deferrals")
