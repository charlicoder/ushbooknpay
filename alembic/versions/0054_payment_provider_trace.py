"""Trace which payment provider / source / link a booking or payment was made through.

Adds three nullable columns to ``bookings`` and ``payments``:

* ``payment_provider_group`` – top-level provider family (e.g. ``Tap``, ``MyFatoorah``)
* ``payment_source``         – exact gateway source used (e.g. ``tap_knet``, ``tap_card``,
                               ``tap_applepay``, ``deema``, ``myfatoorah``)
* ``payment_link_id``        – the ``payment_links.id`` the payment was made through

Existing columns (payment_provider / payment_gateway / payment_method / payment_through)
are untouched, so all current behaviour is preserved.

Revision ID: 0054_payment_provider_trace
Revises: 0053_booking_services_array
Create Date: 2026-10-26
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0054_payment_provider_trace"
down_revision: Union[str, None] = "0053_booking_services_array"
branch_labels = None
depends_on = None

_TABLES = ("bookings", "payments")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS payment_provider_group VARCHAR(30)")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS payment_source VARCHAR(50)")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS payment_link_id UUID")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_payment_provider_group ON {table} (payment_provider_group)"
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_payment_source ON {table} (payment_source)")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_payment_source")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_payment_provider_group")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS payment_link_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS payment_source")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS payment_provider_group")
