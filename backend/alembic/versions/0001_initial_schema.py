"""Initial schema — all tables for the full rebuild vision.

Revision ID: 0001
Revises:
Create Date: 2026-09-19

Chunk 1: creates the complete schema. Nothing writes to it until Chunk 2.
"""

from __future__ import annotations

from alembic import op

from polycopy.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
