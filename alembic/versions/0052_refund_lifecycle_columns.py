"""Add refund lifecycle / idempotency columns to refunds table.

Revision ID: 0052_refund_lifecycle_columns
Revises: 0051_add_payment_voucher_number
Create Date: 2026-10-16
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0052_refund_lifecycle_columns"
down_revision: Union[str, None] = "0051_add_payment_voucher_number"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("idempotency_key", sa.String(150)),
    ("credit_note_id", sa.String(64)),
    ("completed_at", sa.DateTime(timezone=True)),
    ("failure_reason", sa.Text()),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "refunds" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("refunds")}
    for name, col_type in _COLUMNS:
        if name not in cols:
            op.add_column("refunds", sa.Column(name, col_type, nullable=True))

    indexes = {i["name"] for i in inspector.get_indexes("refunds")}
    if "uq_refunds_idempotency_key" not in indexes:
        op.create_index(
            "uq_refunds_idempotency_key", "refunds", ["idempotency_key"], unique=True
        )

    # Historical refunds were recorded as completed at creation time.
    op.execute(
        sa.text(
            "UPDATE refunds SET completed_at = COALESCE(processed_at, created_at) "
            "WHERE status = 'completed' AND completed_at IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "refunds" not in inspector.get_table_names():
        return
    indexes = {i["name"] for i in inspector.get_indexes("refunds")}
    if "uq_refunds_idempotency_key" in indexes:
        op.drop_index("uq_refunds_idempotency_key", table_name="refunds")
    cols = {c["name"] for c in inspector.get_columns("refunds")}
    for name, _ in _COLUMNS:
        if name in cols:
            op.drop_column("refunds", name)
