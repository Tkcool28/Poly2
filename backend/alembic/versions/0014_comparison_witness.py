"""Add comparison witness only. Retain evidence on downgrade (no silent erasure).

Revision ID: 0014
Revises: 0013
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS comparison_transaction_witness (
        transaction_id text PRIMARY KEY, binding_sha256 text NOT NULL,
        payload text NOT NULL, payload_sha256 text NOT NULL
    )""")


def downgrade() -> None:
    # Reverting source wiring does not authorize deleting retained scientific
    # authority. The table is inert without the opt-in observer; upgrade is safe.
    pass
