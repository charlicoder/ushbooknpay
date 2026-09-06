"""add service_name to loyalty tracker and reward

Revision ID: 0010_add_service_name_loyalty
Revises: 4db04c0548bd
Create Date: 2026-08-31

Adds a VARCHAR(255) NOT NULL DEFAULT '' column `service_name` to:
  - promotions_loyalty_tracker
  - promotions_loyalty_reward

The server_default='' means existing rows get an empty string without
requiring a data backfill. New rows will have the name populated from
the booking.confirmed event payload.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision: str = "0010_add_service_name_loyalty"
down_revision: Union[str, None] = "4db04c0548bd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "promotions_loyalty_tracker",
        sa.Column(
            "service_name",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "promotions_loyalty_reward",
        sa.Column(
            "service_name",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("promotions_loyalty_reward", "service_name")
    op.drop_column("promotions_loyalty_tracker", "service_name")
