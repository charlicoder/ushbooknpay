"""Add addons_duration to bookings table.

Revision ID: 0013_add_addons_duration
Revises: 0012_total_dur_base_price
Create Date: 2026-09-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_add_addons_duration"
down_revision: Union[str, None] = "0012_total_dur_base_price"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # addons_duration — total minutes contributed by selected add-ons, nullable
    op.add_column(
        "bookings",
        sa.Column("addons_duration", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bookings", "addons_duration")
