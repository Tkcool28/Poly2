"""PR-E: realistic paper execution evidence on signals + paper_orders.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20

docs/paper-execution-model.md requires every paper order to record the
t0/t1/t2 timestamps, the detection-time book, fill price vs source price,
depth consumed, and miss reasons. Signals become idempotent on the source
trade's canonical identity so detection can never double-fire.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _needs_batch() -> bool:
    """Batch mode only on live SQLite; plain ops elsewhere — offline
    --sql rendering has no live DB to reflect a batch from."""
    return (
        not context.is_offline_mode()
        and op.get_bind().dialect.name == "sqlite"
    )


def upgrade() -> None:
    # --- signals ----------------------------------------------------------
    op.add_column("signals", sa.Column("source_trade_id", sa.String(160)))
    op.add_column("signals", sa.Column("source_price", sa.Numeric(10, 6)))
    op.add_column("signals", sa.Column("t0_traded_at", sa.DateTime(timezone=True)))
    op.add_column("signals", sa.Column("t1_detected_at", sa.DateTime(timezone=True)))
    # Legacy pre-PR-E signal rows have no canonical source identity
    # (source_trade_id NULL). They are placeholders, not evidence: remove
    # them. Detection is idempotent, so any still-eligible trade is
    # re-signaled from its Trade row on the next cycle. Leaving NULLs
    # behind would poison "NOT IN (SELECT source_trade_id)" (NULL
    # semantics suppress ALL future signals) — PR #7 hardening.
    op.execute("DELETE FROM signals WHERE source_trade_id IS NULL")
    # Source identity is mandatory from here on.
    if _needs_batch():
        with op.batch_alter_table("signals") as batch_op:
            batch_op.alter_column(
                "source_trade_id", existing_type=sa.String(160), nullable=False
            )
            batch_op.create_unique_constraint(
                "uq_signals_source_trade", ["source_trade_id"]
            )
    else:
        op.alter_column(
            "signals", "source_trade_id", existing_type=sa.String(160), nullable=False
        )
        op.create_unique_constraint(
            "uq_signals_source_trade", "signals", ["source_trade_id"]
        )

    # --- paper_orders ------------------------------------------------------
    if _needs_batch():
        with op.batch_alter_table("paper_orders") as batch_op:
            batch_op.alter_column(
                "idempotency_key",
                type_=sa.String(200),
                existing_type=sa.String(80),
                existing_nullable=False,
            )
    else:
        op.alter_column(
            "paper_orders",
            "idempotency_key",
            type_=sa.String(200),
            existing_type=sa.String(80),
            existing_nullable=False,
        )
    op.add_column("paper_orders", sa.Column("t2_decided_at", sa.DateTime(timezone=True)))
    op.add_column("paper_orders", sa.Column("fill_price", sa.Numeric(10, 6)))
    op.add_column("paper_orders", sa.Column("filled_size", sa.Numeric(20, 6)))
    op.add_column("paper_orders", sa.Column("fee", sa.Numeric(20, 6)))
    op.add_column("paper_orders", sa.Column("book_snapshot", sa.JSON()))
    op.add_column("paper_orders", sa.Column("miss_reason", sa.String(60)))


def downgrade() -> None:
    op.drop_column("paper_orders", "miss_reason")
    op.drop_column("paper_orders", "book_snapshot")
    op.drop_column("paper_orders", "fee")
    op.drop_column("paper_orders", "filled_size")
    op.drop_column("paper_orders", "fill_price")
    op.drop_column("paper_orders", "t2_decided_at")
    if _needs_batch():
        with op.batch_alter_table("paper_orders") as batch_op:
            batch_op.alter_column(
                "idempotency_key",
                type_=sa.String(80),
                existing_type=sa.String(200),
                existing_nullable=False,
            )
        with op.batch_alter_table("signals") as batch_op:
            batch_op.drop_constraint("uq_signals_source_trade", type_="unique")
            batch_op.alter_column(
                "source_trade_id", existing_type=sa.String(160), nullable=True
            )
    else:
        op.alter_column(
            "paper_orders",
            "idempotency_key",
            type_=sa.String(80),
            existing_type=sa.String(200),
            existing_nullable=False,
        )
        op.drop_constraint("uq_signals_source_trade", "signals", type_="unique")
        op.alter_column(
            "signals", "source_trade_id", existing_type=sa.String(160), nullable=True
        )
    op.drop_column("signals", "t1_detected_at")
    op.drop_column("signals", "t0_traded_at")
    op.drop_column("signals", "source_price")
    op.drop_column("signals", "source_trade_id")
