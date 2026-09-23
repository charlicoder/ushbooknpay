"""Drop promotions_loyalty_reward and promotions_loyalty_tracker tables.

Revision ID: 0037_drop_loyalty_tables
Revises: 0036_loyalty_system_overhaul
Create Date: 2026-09-23
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_drop_loyalty_tables"
down_revision: Union[str, None] = "0036_loyalty_system_overhaul"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # 1. Drop promotions_loyalty_reward (has FK to bookings, so drop before tracker)
    if "promotions_loyalty_reward" in tables:
        op.drop_table("promotions_loyalty_reward")

    # 2. Drop promotions_loyalty_tracker
    if "promotions_loyalty_tracker" in tables:
        op.drop_table("promotions_loyalty_tracker")


def downgrade() -> None:
    pass
