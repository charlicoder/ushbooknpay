"""merge voucher and gifts v2 heads

Revision ID: 0032_merge_voucher_gifts_v2
Revises: 5eb04c0548be, 0031_digital_product_data
Create Date: 2026-09-20 03:08:17.468623

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0032_merge_voucher_gifts_v2'
down_revision: Union[str, None] = ('5eb04c0548be', '0031_digital_product_data')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
