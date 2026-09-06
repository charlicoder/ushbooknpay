"""Add created_by to bookings table.

Revision ID: 0014_add_created_by
Revises: 0013_add_addons_duration
Create Date: 2026-09-02

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_add_created_by"
down_revision: Union[str, None] = "0013_add_addons_duration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add created_by column to bookings (nullable, stores User UUID of API caller)."""
    op.add_column(
        "bookings",
        sa.Column("created_by", sa.String(255), nullable=True, server_default=None),
    )


def downgrade() -> None:
    """Remove created_by column from bookings."""
    op.drop_column("bookings", "created_by")
