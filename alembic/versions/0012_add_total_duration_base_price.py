"""Add total_duration and base_price to bookings table.

Revision ID: 0012_total_dur_base_price
Revises: 0011_add_loyalty_fields_bookings
Create Date: 2026-09-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_total_dur_base_price"
down_revision: Union[str, None] = "0011_add_loyalty_fields_bookings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # total_duration — integer, nullable (existing rows default to NULL)
    op.add_column(
        "bookings",
        sa.Column("total_duration", sa.Integer(), nullable=True),
    )
    # base_price — service base price at booking time, nullable
    op.add_column(
        "bookings",
        sa.Column("base_price", sa.Numeric(precision=10, scale=3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bookings", "base_price")
    op.drop_column("bookings", "total_duration")
