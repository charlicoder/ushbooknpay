"""Make gift_vouchers.service_id optional (nullable).

Revision ID: 0030_voucher_service_id_optional
Revises: 0029_add_delivery_and_items
Create Date: 2026-09-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0030_voucher_service_id_optional"
down_revision: Union[str, None] = "0029_add_delivery_and_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "gift_vouchers",
        "service_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "gift_vouchers",
        "service_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
